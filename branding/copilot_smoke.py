#!/usr/bin/env python3
"""Headless smoke test for the Copilot frontend bundle (hanzo-copilot.js).

Loads the REAL bundle into a page against a stub window.app / window.LiteGraph
(a minimal LiteGraph subset) and asserts:
  1. window.HanzoCopilot.applyOps mutates the mock graph: add / connect /
     set_widget / set_prompt / move / delete / layout / queue.
  2. The sidebar tab registers and its render(el) builds the chat panel DOM.

This is the frontend half of the copilot tests. Live end-to-end (the panel
talking to POST /v1/copilot/chat) needs the one server restart that registers
the backend route; the bundle itself is exercised here without a server.

Run:  python3 branding/copilot_smoke.py        (needs: pip install playwright)
"""
import os
import sys

from playwright.sync_api import sync_playwright

BUNDLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hanzo-copilot.js")

# Stub LiteGraph + app: the smallest surface applyOps touches.
STUB = """
window.__connections = [];
window.__queued = null;
window.LiteGraph = {
  _id: 1000,
  createNode(type) {
    return {
      type, id: ++window.LiteGraph._id, pos: [0, 0], size: [200, 100],
      inputs: [{ name: 'image' }, { name: 'model' }],
      outputs: [{ name: 'IMAGE' }],
      widgets: [{ name: 'text', value: '' }, { name: 'seed', value: 0 }],
      connect(outIdx, target, inIdx) {
        window.__connections.push({ from: this.id, outIdx, to: target.id, inIdx });
        return true;
      },
    };
  },
};
(function () {
  var nodes = [];
  window.__nodes = nodes;
  window.app = {
    extensionManager: { registerSidebarTab(t) { window.__tab = t; } },
    graph: {
      _nodes: nodes, links: {},
      add(n) { nodes.push(n); },
      remove(n) { var i = nodes.indexOf(n); if (i >= 0) nodes.splice(i, 1); },
      getNodeById(id) { return nodes.find(function (n) { return n.id == id; }) || null; },
      setDirtyCanvas() {},
    },
    queuePrompt(n) { window.__queued = n; },
    async graphToPrompt() { return { output: {} }; },
  };
})();
"""

OPS = [
    {"op": "add_node", "type": "CLIPTextEncode", "id": "pos", "pos": [10, 20]},
    {"op": "add_node", "type": "SaveImage", "id": "save"},
    {"op": "set_prompt", "node_id": "pos", "text": "a red one-piece at golden hour"},
    {"op": "set_widget", "node_id": "save", "name": "seed", "value": 7},
    {"op": "connect", "from_node": "pos", "from_slot": "IMAGE", "to_node": "save", "to_slot": "image"},
    {"op": "move_node", "node_id": "save", "pos": [300, 40]},
    {"op": "layout"},
    {"op": "queue"},
    {"op": "delete_node", "node_id": "pos"},
    {"op": "bogus", "node_id": "pos"},
]


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content("<!doctype html><html><head></head><body></body></html>")
        page.evaluate(STUB)
        page.add_script_tag(path=BUNDLE)

        assert page.evaluate("typeof window.HanzoCopilot") == "object", "HanzoCopilot not exposed"

        # Apply the op batch against the mock graph.
        res = page.evaluate("(ops) => window.HanzoCopilot.applyOps(ops)", OPS)
        print("applyOps ->", res)

        nodes = page.evaluate("window.__nodes.map(function(n){return {id:n.id,type:n.type,pos:n.pos};})")
        conns = page.evaluate("window.__connections")
        queued = page.evaluate("window.__queued")
        save_seed = page.evaluate(
            "(function(){var n=window.__nodes.find(function(x){return x.type=='SaveImage';});"
            "return n.widgets.find(function(w){return w.name=='seed';}).value;})()"
        )

        # 2 added, then 1 deleted -> only SaveImage remains.
        types = sorted(n["type"] for n in nodes)
        assert types == ["SaveImage"], f"expected only SaveImage after delete, got {types}"
        assert len(conns) == 1 and conns[0]["outIdx"] == 0 and conns[0]["inIdx"] == 0, f"bad connect: {conns}"
        assert save_seed == 7, f"set_widget failed: seed={save_seed}"
        assert queued == 0, f"queue op did not fire: {queued}"
        # bogus op is skipped, not thrown, and noted.
        assert any("bogus" in note for note in res["notes"]), f"bogus op not noted: {res['notes']}"
        # 8 good ops applied (add,add,set_prompt,set_widget,connect,move,layout,queue,delete = 9);
        # bogus skipped.
        assert res["applied"] == 9, f"expected 9 applied, got {res['applied']}"

        # Sidebar tab registered; render builds the panel.
        page.wait_for_function("() => window.__tab !== undefined", timeout=5000)
        tab = page.evaluate("({id: window.__tab.id, title: window.__tab.title, icon: window.__tab.icon})")
        assert tab["id"] == "hanzo-copilot", f"bad tab id: {tab}"
        panel = page.evaluate(
            "(function(){var d=document.createElement('div');document.body.appendChild(d);"
            "window.__tab.render(d);return {panel:d.classList.contains('hanzo-copilot'),"
            "chips:d.querySelectorAll('.hz-chip').length,input:!!d.querySelector('textarea')};})()"
        )
        assert panel["panel"] and panel["input"] and panel["chips"] >= 3, f"panel not built: {panel}"

        browser.close()
        print("tab ->", tab)
        print("panel ->", panel)
        print("PASS: copilot frontend smoke")
        return 0


if __name__ == "__main__":
    sys.exit(main())
