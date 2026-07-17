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
  var chatBtn = el("button", { class: "hz-pill hz-chat", title: "Chat" }, '💬<span class="hz-lbl"> Chat</span>');
  bar.appendChild(toggle);
  if (tmplBtn) bar.appendChild(tmplBtn);
  bar.appendChild(wallet); bar.appendChild(gpus); bar.appendChild(chatBtn);

  var gpuPanel = el("div", { id: "hzgpus" }, '<h4>GPUs</h4><div id="hznodes"></div>');
  var chat = el("aside", { id: "hzchat" });
  chat.innerHTML =
    '<div class="hz-h"><span class="hz-av" id="hzav">·</span>' +
    '<span class="hz-who"><b id="hzuser">…</b><span id="hzorg"></span></span>' +
    '<button class="hz-x" id="hzclose" title="Close">×</button></div>' +
    '<div id="hzmsgs"></div>' +
    '<form id="hzform"><input id="hzin" placeholder="Ask the copilot…" autocomplete="off"><button type="submit">Send</button></form>';

  function mount() {
    document.body.appendChild(bar);
    document.body.appendChild(gpuPanel);
    document.body.appendChild(chat);
    wire();
    loadSession(); loadWallet(); loadGpus();
    if (!STUDIO) editorBoot();
  }

  // ── chat: the ONE path — POST /v1/copilot/chat; ops applied via the editor's
  // existing applier when present (no second chat mechanism) ────────────────────
  var HISTORY = [];
  function addMsg(role, text) {
    var m = el("div", { class: "hz-m " + role }, esc(text));
    $("hzmsgs").appendChild(m); $("hzmsgs").scrollTop = $("hzmsgs").scrollHeight;
    return m;
  }
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
  async function send(text) {
    HISTORY.push({ role: "user", content: text });
    addMsg("u", text);
    var pending = addMsg("sys", "…");
    var payload = Object.assign({ messages: HISTORY.slice(-20) }, await graphContext());
    var r = await getJSON("/v1/copilot/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    pending.remove();
    var reply = (r.body && r.body.reply) || (r.body && r.body.error) || (r.ok ? "" : "chat failed (" + r.status + ")");
    if (reply) { HISTORY.push({ role: "assistant", content: reply }); addMsg("a", reply); }
    var ops = (r.body && r.body.ops) || [];
    if (ops.length && window.HanzoCopilot && typeof window.HanzoCopilot.applyOps === "function") {
      try { window.HanzoCopilot.applyOps(ops); } catch (e) { addMsg("sys", "couldn't apply changes: " + e.message); }
    } else if (ops.length && STUDIO) {
      addMsg("sys", "Open the Editor to apply " + ops.length + " graph change" + (ops.length > 1 ? "s" : "") + ".");
    }
  }

  function wire() {
    chatBtn.onclick = function () { chat.classList.toggle("on"); if (chat.classList.contains("on")) $("hzin").focus(); };
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
      var sub = (n.online ? "online" : "offline") +
        (s.running ? " · running: " + esc(String(s.running).slice(0, 40)) : (n.online ? " · idle" : "")) +
        " · queue " + s.depth;
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

  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();
