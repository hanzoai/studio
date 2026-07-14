/* Hanzo Studio deep-link — open a saved workflow (and optionally run it) straight
 * from a URL, so a link in the gallery / console / chat lands you on the graph
 * ready to render.
 *
 * Params (query string or hash, e.g. ?org=karma&workflow=fashion/shoot_valentina.json&run=1):
 *   org        active org to scope userdata + render output to (sets the studio
 *              session org cookie BEFORE loading, so a re-run dispatches to the
 *              org's BYO-GPU and lands the output in that org's gallery).
 *   workflow   path under the user's workflows/ dir (e.g. "fashion/shoot_x.json").
 *   run        "1" to auto-queue the graph once loaded (re-run in one click).
 *
 * Injected as the last <script> by branding/patch_deeplink.py so window.app
 * exists. Guarded end-to-end: a bad param logs and no-ops, never throws.
 */
(function () {
  "use strict";
  var VERSION = "1";
  function app() { return window.app; }

  function params() {
    var out = {};
    var q = new URLSearchParams(window.location.search);
    q.forEach(function (v, k) { out[k] = v; });
    if (window.location.hash && window.location.hash.length > 1) {
      var h = new URLSearchParams(window.location.hash.slice(1));
      h.forEach(function (v, k) { if (out[k] == null) out[k] = v; });
    }
    return out;
  }

  function waitFor(cond, ms) {
    return new Promise(function (resolve, reject) {
      var t0 = Date.now();
      (function poll() {
        var v; try { v = cond(); } catch (e) { v = null; }
        if (v) return resolve(v);
        if (Date.now() - t0 > ms) return reject(new Error("timeout"));
        setTimeout(poll, 150);
      })();
    });
  }

  function toast(msg, ok) {
    var el = document.createElement("div");
    el.textContent = msg;
    el.style.cssText =
      "position:fixed;top:14px;left:50%;transform:translateX(-50%);z-index:99999;" +
      "padding:8px 14px;border-radius:8px;font:13px system-ui;color:#fff;" +
      "background:" + (ok ? "#1f7a4d" : "#8a1f1f") + ";box-shadow:0 4px 16px rgba(0,0,0,.35)";
    document.body.appendChild(el);
    setTimeout(function () { el.style.transition = "opacity .4s"; el.style.opacity = "0"; }, 3200);
    setTimeout(function () { el.remove(); }, 3700);
  }

  async function setOrg(org) {
    // Set the studio active org so userdata + render output scope to it. Same-origin
    // fetch carries cookies; the response Set-Cookie updates studio_active_org.
    var r = await fetch("/v1/session/org", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ org: org }),
    });
    if (!r.ok) throw new Error("org switch failed (" + r.status + ")");
  }

  async function loadWorkflow(wf) {
    var key = "workflows/" + String(wf).replace(/^\/+/, "");
    var r = await fetch("/userdata/" + encodeURIComponent(key), { credentials: "same-origin" });
    if (!r.ok) throw new Error("workflow not found: " + wf + " (" + r.status + ")");
    var data = await r.json();
    await app().loadGraphData(data);
    return key;
  }

  async function run() {
    var p = params();
    if (!p.workflow && !p.org) return; // nothing to do — normal studio load
    await waitFor(function () { return app() && typeof app().loadGraphData === "function"; }, 30000);
    try {
      if (p.org) { await setOrg(p.org); }
      if (p.workflow) {
        await loadWorkflow(p.workflow);
        toast("Opened " + p.workflow + (p.org ? " · " + p.org : ""), true);
        if (String(p.run) === "1" && typeof app().queuePrompt === "function") {
          // Give the graph a tick to settle before queuing.
          setTimeout(function () {
            try { app().queuePrompt(0); toast("Queued render → your GPU", true); }
            catch (e) { toast("Auto-run failed: " + e.message, false); }
          }, 400);
        }
      } else if (p.org) {
        toast("Switched to " + p.org, true);
      }
    } catch (e) {
      console.warn("[Hanzo deep-link]", e);
      toast(e.message, false);
    }
  }

  run();
  console.log("[Hanzo deep-link] v" + VERSION + " ready");
})();
