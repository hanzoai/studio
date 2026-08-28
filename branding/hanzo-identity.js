/* Hanzo Studio Identity menu — the top-right user menu, driven entirely by the
 * IAM-validated session the backend exposes at GET /v1/session.
 *
 * It reads {user,email,org,orgs[],project,projects[],console_url,iam_url} and
 * renders a pill (avatar/initials + name). Opening it shows the signed-in user,
 * an org switcher (POST /v1/session/org -> reload, re-scoped to that org for
 * tenancy + output paths), a project switcher (POST /v1/session/project), a
 * link back to Hanzo Console, and Logout (GET /logout -> clears the session
 * cookie -> IAM end-session -> /login).
 *
 * Self-contained and SPA-agnostic: a fixed element in the menu-bar corner, so it
 * never fights the Vue app. Injected as a deferred <script> by patch_identity.py.
 * window.HanzoIdentity is exposed for headless tests.
 */
(function () {
  "use strict";
  if (window.__hanzoIdentityLoaded) return;
  window.__hanzoIdentityLoaded = true;
  var VERSION = "3";

  // ------------------------------------------------------------- dom helper
  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === "class") n.className = attrs[k];
      else if (k === "text") n.textContent = attrs[k];
      else if (k === "html") n.innerHTML = attrs[k];
      else if (k.slice(0, 2) === "on") n[k.toLowerCase()] = attrs[k];
      else n.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }

  function initials(name, email) {
    var src = (name || email || "?").trim();
    var parts = src.split(/[\s@._-]+/).filter(Boolean);
    if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
    return src.slice(0, 2).toUpperCase();
  }

  // ------------------------------------------------------------- api
  function getSession() {
    return fetch("./v1/session", { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (r) {
        // 401 is the real "not signed in" (auth on, no valid token) -> Sign in.
        // A 200 body is always a usable session (a real IAM user in the cloud, or
        // the local single-user stub when auth is off) -> render the pill.
        if (r.status === 401) return { signedOut: true };
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });
  }
  function post(path, body) {
    return fetch("./" + path, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) { return r.json().catch(function () { return {}; }); });
  }

  var state = { session: null, open: false };

  function switchOrg(org) {
    if (!org || (state.session && org === state.session.org)) return;
    post("v1/session/org", { org: org }).then(function () { location.reload(); });
  }
  function switchProject(p) {
    if (!p || (state.session && p === state.session.project)) return;
    post("v1/session/project", { project: p }).then(function () { location.reload(); });
  }

  // ------------------------------------------------------------- render
  function close() { state.open = false; paint(); }

  function menuPanel(s) {
    var rows = [];
    rows.push(el("div", { class: "hzid-id" }, [
      el("div", { class: "hzid-avatar-lg", text: initials(s.user, s.email) }),
      el("div", { class: "hzid-idtext" }, [
        el("div", { class: "hzid-name", text: s.user || "User" }),
        el("div", { class: "hzid-email", text: s.email || "" }),
      ]),
    ]));

    // Organization switcher
    rows.push(el("div", { class: "hzid-label", text: "Organization" }));
    (s.orgs || [s.org]).forEach(function (o) {
      rows.push(el("button", {
        class: "hzid-item" + (o === s.org ? " hzid-active" : ""),
        onclick: function () { switchOrg(o); },
      }, [
        el("span", { class: "hzid-tick", text: o === s.org ? "✓" : "" }),
        el("span", { text: o }),
      ]));
    });

    // Project (workflow workspace) switcher
    if (s.projects && s.projects.length) {
      rows.push(el("div", { class: "hzid-label", text: "Project" }));
      s.projects.forEach(function (p) {
        rows.push(el("button", {
          class: "hzid-item" + (p === s.project ? " hzid-active" : ""),
          onclick: function () { switchProject(p); },
        }, [
          el("span", { class: "hzid-tick", text: p === s.project ? "✓" : "" }),
          el("span", { text: p }),
        ]));
      });
    }

    rows.push(el("div", { class: "hzid-sep" }));
    // Exit the advanced node editor back to the content-forward Studio Home.
    // Without this there is NO way out of Advanced mode (the node editor is the
    // full-screen ComfyUI SPA) — the user gets stuck.
    rows.push(el("a", { class: "hzid-item hzid-link", href: "/studio" }, [
      el("span", { class: "hzid-tick", text: "←" }), el("span", { text: "Exit to Studio Home" }),
    ]));
    rows.push(el("a", { class: "hzid-item hzid-link", href: s.console_url || "https://console.hanzo.ai", target: "_blank", rel: "noopener" }, [
      el("span", { class: "hzid-tick", text: "↗" }), el("span", { text: "Console" }),
    ]));
    rows.push(el("a", { class: "hzid-item hzid-logout", href: "/logout" }, [
      el("span", { class: "hzid-tick", text: "⏻" }), el("span", { text: "Logout" }),
    ]));
    return el("div", { class: "hzid-panel" }, rows);
  }

  function paint() {
    var root = document.getElementById("hanzo-identity");
    if (!root) return;
    root.innerHTML = "";
    var s = state.session;
    if (!s) return;

    if (s.signedOut) {
      root.appendChild(el("a", { class: "hzid-pill hzid-signin", href: "/login" }, [
        el("span", { text: "Sign in with Hanzo" }),
      ]));
      return;
    }

    var pill = el("button", { class: "hzid-pill", title: (s.email || s.user) + " · " + s.org, onclick: function (e) { e.stopPropagation(); state.open = !state.open; paint(); } }, [
      el("span", { class: "hzid-avatar", text: initials(s.user, s.email) }),
      el("span", { class: "hzid-pillname", text: s.user || "User" }),
      el("span", { class: "hzid-org", text: s.org || "" }),
      el("span", { class: "hzid-caret", text: "▾" }),
    ]);
    root.appendChild(pill);
    if (state.open) root.appendChild(menuPanel(s));
  }

  // dismiss on outside click
  document.addEventListener("click", function (e) {
    if (state.open && !e.target.closest("#hanzo-identity")) close();
  });

  // ------------------------------------------------------------- boot
  function mount() {
    if (document.getElementById("hanzo-identity")) return;
    // Always-visible "Exit to Studio" button so Advanced mode is never a trap.
    // Fixed top-left, above the node editor; independent of the user menu.
    if (!document.getElementById("hanzo-exit-advanced")) {
      var exit = el("a", {
        id: "hanzo-exit-advanced", href: "/studio", title: "Exit Advanced — back to Studio Home",
        style: "position:fixed;top:10px;left:12px;z-index:100000;display:flex;align-items:center;gap:6px;" +
               "background:rgba(20,20,24,.92);color:#ececf1;border:1px solid #33333c;border-radius:8px;" +
               "padding:6px 11px;font:600 12px/1 Zen,system-ui,sans-serif;text-decoration:none;cursor:pointer;backdrop-filter:blur(8px)"
      }, [el("span", { text: "←" }), el("span", { text: "Studio" })]);
      document.body.appendChild(exit);
    }
    var root = el("div", { id: "hanzo-identity", class: "hanzo-identity" });
    document.body.appendChild(root);
    getSession().then(function (s) {
      state.session = s; paint();
      console.log("[Hanzo Identity] mounted v" + VERSION + (s && s.user ? " as " + s.user : ""));
    }).catch(function (err) {
      // /v1/session not registered yet (backend pre-restart) — stay silent.
      console.warn("[Hanzo Identity] session unavailable:", err && err.message);
    });
  }

  // testability: let a test set the session and repaint without the network.
  window.HanzoIdentity = {
    VERSION: VERSION,
    mount: mount,
    reload: function () { return getSession().then(function (s) { state.session = s; paint(); return s; }); },
    _set: function (s) { state.session = s; paint(); },
  };

  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();
