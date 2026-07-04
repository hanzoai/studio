/* Hanzo Studio Copilot — a sidebar chat that reads and MUTATES the workflow graph.
 *
 * You talk to the canvas in natural language ("add a 4x upscale after the save
 * image"). The backend (POST /v1/copilot/chat) decides graph operations; THIS
 * file applies them against app.graph (LiteGraph). Every op is guarded — an
 * unknown node type or bad slot is skipped and noted, never thrown.
 *
 * Injected as the last <script> by branding/patch_copilot.py so window.app exists
 * (or is about to — we poll). window.HanzoCopilot.applyOps is exposed immediately
 * for programmatic use and headless tests.
 */
(function () {
  "use strict";
  var VERSION = "1";

  // ---------------------------------------------------------------- helpers
  function app() { return window.app; }
  function graph() { return window.app && window.app.graph; }
  function LG() { return window.LiteGraph; }

  function waitFor(pred, timeoutMs) {
    return new Promise(function (resolve, reject) {
      var t0 = Date.now();
      (function tick() {
        var v = pred();
        if (v) return resolve(v);
        if (Date.now() - t0 > timeoutMs) return reject(new Error("timeout"));
        setTimeout(tick, 120);
      })();
    });
  }

  function resolveSlot(list, slot) {
    // slot may be an index (number) or a slot name (string). Returns an index.
    if (typeof slot === "number") return slot;
    if (slot == null) return 0;
    var name = String(slot).toLowerCase();
    for (var i = 0; i < (list || []).length; i++) {
      var s = list[i];
      var n = (s && (s.name || s.label)) || s;
      if (String(n).toLowerCase() === name) return i;
    }
    return 0;
  }

  // ---------------------------------------------------------------- applier
  // Applies one batch of ops. Returns {applied, notes[]}. New nodes are referenced
  // in later ops by the id label the model gave them in add_node.
  function applyOps(ops) {
    var g = graph();
    var notes = [];
    var applied = 0;
    if (!g) { return { applied: 0, notes: ["no graph"] }; }
    var local = {}; // model-label / id -> node

    function resolve(id) {
      if (id == null) return null;
      if (local[id]) return local[id];
      var n = g.getNodeById(typeof id === "string" && /^\d+$/.test(id) ? Number(id) : id);
      if (!n && typeof id === "number") n = g.getNodeById(id);
      return n || null;
    }
    function setWidget(node, name, value) {
      if (!node.widgets) return false;
      var lname = String(name).toLowerCase();
      for (var i = 0; i < node.widgets.length; i++) {
        var w = node.widgets[i];
        if (w.name && w.name.toLowerCase() === lname) {
          w.value = value;
          if (typeof w.callback === "function") { try { w.callback(value); } catch (e) {} }
          return true;
        }
      }
      return false;
    }

    (ops || []).forEach(function (op, idx) {
      try {
        switch (op.op) {
          case "add_node": {
            var Lg = LG();
            var node = Lg && Lg.createNode ? Lg.createNode(op.type) : null;
            if (!node) { notes.push("skip add_node: unknown type " + op.type); break; }
            g.add(node);
            if (Array.isArray(op.pos)) node.pos = [op.pos[0], op.pos[1]];
            if (op.id != null) local[op.id] = node;
            local[node.id] = node;
            applied++;
            break;
          }
          case "set_widget": {
            var n1 = resolve(op.node_id);
            if (!n1) { notes.push("skip set_widget: no node " + op.node_id); break; }
            if (!setWidget(n1, op.name, op.value)) { notes.push("skip set_widget: no widget " + op.name + " on " + op.node_id); break; }
            applied++;
            break;
          }
          case "set_prompt": {
            var n2 = resolve(op.node_id);
            if (!n2 || !n2.widgets) { notes.push("skip set_prompt: no node " + op.node_id); break; }
            var target = null;
            for (var j = 0; j < n2.widgets.length; j++) {
              if (n2.widgets[j].name && n2.widgets[j].name.toLowerCase() === "text") { target = n2.widgets[j]; break; }
            }
            if (!target) target = n2.widgets.find(function (w) { return typeof w.value === "string"; });
            if (!target) { notes.push("skip set_prompt: no text widget on " + op.node_id); break; }
            target.value = op.text;
            if (typeof target.callback === "function") { try { target.callback(op.text); } catch (e) {} }
            applied++;
            break;
          }
          case "connect": {
            var from = resolve(op.from_node), to = resolve(op.to_node);
            if (!from || !to) { notes.push("skip connect: missing node"); break; }
            var oi = resolveSlot(from.outputs, op.from_slot);
            var ii = resolveSlot(to.inputs, op.to_slot);
            from.connect(oi, to, ii);
            applied++;
            break;
          }
          case "move_node": {
            var n3 = resolve(op.node_id);
            if (!n3 || !Array.isArray(op.pos)) { notes.push("skip move_node: " + op.node_id); break; }
            n3.pos = [op.pos[0], op.pos[1]];
            applied++;
            break;
          }
          case "delete_node": {
            var n4 = resolve(op.node_id);
            if (!n4) { notes.push("skip delete_node: no node " + op.node_id); break; }
            g.remove(n4);
            applied++;
            break;
          }
          case "layout": {
            layout();
            applied++;
            break;
          }
          case "queue": {
            if (app() && typeof app().queuePrompt === "function") { app().queuePrompt(0); applied++; }
            else notes.push("skip queue: unavailable");
            break;
          }
          default:
            notes.push("skip unknown op " + op.op);
        }
      } catch (e) {
        notes.push("error on op " + idx + " (" + op.op + "): " + e.message);
      }
    });

    if (app() && app().graph && app().graph.setDirtyCanvas) app().graph.setDirtyCanvas(true, true);
    return { applied: applied, notes: notes };
  }

  // Simple left-to-right columnar tidy: column = longest link-path from a source.
  function layout() {
    var g = graph();
    if (!g || !g._nodes) return;
    var nodes = g._nodes;
    var col = {};
    nodes.forEach(function (n) { col[n.id] = 0; });
    for (var pass = 0; pass < nodes.length; pass++) {
      var changed = false;
      nodes.forEach(function (n) {
        (n.inputs || []).forEach(function (inp) {
          if (inp && inp.link != null && g.links && g.links[inp.link]) {
            var src = g.links[inp.link].origin_id;
            if (col[src] != null && col[n.id] < col[src] + 1) { col[n.id] = col[src] + 1; changed = true; }
          }
        });
      });
      if (!changed) break;
    }
    var COLW = 340, ROWH = 240, rowOf = {};
    nodes.slice().sort(function (a, b) { return (col[a.id] - col[b.id]) || (a.id - b.id); }).forEach(function (n) {
      var c = col[n.id] || 0;
      rowOf[c] = (rowOf[c] || 0);
      n.pos = [80 + c * COLW, 80 + rowOf[c] * ROWH];
      rowOf[c]++;
    });
    if (g.setDirtyCanvas) g.setDirtyCanvas(true, true);
  }

  window.HanzoCopilot = { applyOps: applyOps, layout: layout, VERSION: VERSION };

  // ---------------------------------------------------------------- catalog
  var _objectInfo = null;
  function getObjectInfo() {
    if (_objectInfo) return Promise.resolve(_objectInfo);
    return fetch("./object_info").then(function (r) { return r.json(); }).then(function (j) {
      _objectInfo = j; return j;
    });
  }
  function inputTypeStr(spec) {
    if (Array.isArray(spec)) {
      var t = spec[0];
      if (typeof t === "string") return t;
      if (Array.isArray(t)) return "COMBO";
      return "ANY";
    }
    return "ANY";
  }
  function buildCatalog(info, graphJson) {
    var types = Object.keys(info);
    var present = {};
    Object.keys(graphJson || {}).forEach(function (id) {
      var ct = graphJson[id] && graphJson[id].class_type;
      if (ct) present[ct] = true;
    });
    var detail = {};
    Object.keys(present).forEach(function (t) {
      var d = info[t]; if (!d) return;
      var ins = {};
      ["required", "optional"].forEach(function (grp) {
        var block = d.input && d.input[grp];
        if (block) Object.keys(block).forEach(function (name) { ins[name] = inputTypeStr(block[name]); });
      });
      detail[t] = { inputs: ins, outputs: d.output_name || d.output || [] };
    });
    return { types: types, nodes: detail };
  }

  // ---------------------------------------------------------------- toast
  function toast(msg) {
    try {
      var em = app() && app().extensionManager;
      if (em && em.toast && typeof em.toast.add === "function") {
        em.toast.add({ severity: "success", summary: "Copilot", detail: msg, life: 4000 });
        return;
      }
    } catch (e) {}
    var el = document.createElement("div");
    el.className = "hanzo-copilot-toast";
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(function () { el.classList.add("show"); }, 10);
    setTimeout(function () { el.classList.remove("show"); setTimeout(function () { el.remove(); }, 300); }, 3800);
  }

  // ---------------------------------------------------------------- chat state + UI
  var state = { messages: [], busy: false };
  var CHIPS = [
    "Add a 4x upscale after the save image",
    "Connect the latent output to a new VAE decode",
    "Set the positive prompt to a red one-piece on a beach at golden hour",
    "Tidy up the layout",
  ];

  function esc(s) { return String(s).replace(/[&<>]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]; }); }

  function renderMessages(box) {
    box.innerHTML = "";
    state.messages.forEach(function (m) {
      var row = document.createElement("div");
      row.className = "hz-msg hz-" + m.role;
      row.innerHTML = '<div class="hz-bubble">' + esc(m.content).replace(/\n/g, "<br>") + "</div>";
      box.appendChild(row);
    });
    if (state.busy) {
      var t = document.createElement("div");
      t.className = "hz-msg hz-assistant";
      t.innerHTML = '<div class="hz-bubble hz-typing">…</div>';
      box.appendChild(t);
    }
    box.scrollTop = box.scrollHeight;
  }

  async function send(text, box) {
    if (!text || state.busy) return;
    state.messages.push({ role: "user", content: text });
    state.busy = true;
    renderMessages(box);
    try {
      var gp = await app().graphToPrompt();
      var graphJson = (gp && gp.output) || {};
      var info = await getObjectInfo().catch(function () { return {}; });
      var catalog = buildCatalog(info, graphJson);
      var resp = await fetch("./v1/copilot/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: state.messages, graph_json: graphJson, catalog: catalog }),
      });
      var data = await resp.json().catch(function () { return {}; });
      if (!resp.ok) throw new Error(data.error || ("HTTP " + resp.status));
      var reply = data.reply || "(no reply)";
      state.messages.push({ role: "assistant", content: reply });
      state.busy = false;
      renderMessages(box);
      var ops = data.ops || [];
      if (ops.length) {
        var res = applyOps(ops);
        var summary = res.applied + " change" + (res.applied === 1 ? "" : "s") + " applied";
        if (res.notes.length) summary += " · " + res.notes.length + " skipped";
        toast(summary);
        if (res.notes.length) {
          state.messages.push({ role: "assistant", content: res.notes.join("\n") });
          renderMessages(box);
        }
      }
    } catch (e) {
      state.busy = false;
      state.messages.push({ role: "assistant", content: "⚠ " + e.message });
      renderMessages(box);
    }
  }

  function buildUI(el) {
    el.classList.add("hanzo-copilot");
    el.innerHTML =
      '<div class="hz-head"><span class="hz-dot"></span> Copilot</div>' +
      '<div class="hz-messages"></div>' +
      '<div class="hz-chips"></div>' +
      '<div class="hz-input"><textarea rows="2" placeholder="Tell the canvas what to do…"></textarea>' +
      '<button class="hz-send" title="Send">Send</button></div>';
    var box = el.querySelector(".hz-messages");
    var ta = el.querySelector("textarea");
    var chips = el.querySelector(".hz-chips");
    CHIPS.forEach(function (c) {
      var b = document.createElement("button");
      b.className = "hz-chip"; b.textContent = c;
      b.onclick = function () { ta.value = c; ta.focus(); };
      chips.appendChild(b);
    });
    function fire() { var v = ta.value.trim(); ta.value = ""; send(v, box); }
    el.querySelector(".hz-send").onclick = fire;
    ta.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); fire(); }
    });
    if (!state.messages.length) {
      state.messages.push({ role: "assistant", content: "Hi — I can edit this workflow for you. Try a chip below or just tell me what to change." });
    }
    renderMessages(box);
  }

  // ---------------------------------------------------------------- boot
  function register() {
    var a = app();
    if (a && a.extensionManager && typeof a.extensionManager.registerSidebarTab === "function") {
      a.extensionManager.registerSidebarTab({
        id: "hanzo-copilot",
        icon: "pi pi-comments",
        title: "Copilot",
        tooltip: "Chat Copilot — edit the workflow in natural language",
        type: "custom",
        render: function (el) { buildUI(el); },
        destroy: function () {},
      });
      console.log("[Hanzo Copilot] sidebar tab registered v" + VERSION);
      return true;
    }
    return false;
  }

  waitFor(function () { return app() && app().extensionManager; }, 30000)
    .then(register)
    .catch(function () { console.warn("[Hanzo Copilot] app.extensionManager not found; copilot tab not mounted"); });
})();
