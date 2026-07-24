"""The Chat SSE parser lives in middleware/shell.js as two pure functions
(``sseLines`` splits streamed chunks into whole lines across buffer boundaries;
``sseDelta`` maps one line to its content delta). This test extracts them from the
SHIPPED shell and exercises them under node -- chunk-boundary splits, CRLF, [DONE],
keep-alives and malformed lines -- so the one parser the browser runs is the one
under test. Skipped where node is unavailable, keeping ``uv run pytest`` green.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

SHELL = Path(__file__).resolve().parents[2] / "middleware" / "shell.js"


def _fn(src: str, name: str) -> str:
    """Return the source of the top-level `function <name>(...) {...}` by brace match."""
    i = src.index("function " + name + "(")
    depth = 0
    start = src.index("{", i)
    for j in range(start, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError("unbalanced braces extracting " + name)


_CHECKS = r"""
function eq(a, b, m) {
  if (JSON.stringify(a) !== JSON.stringify(b)) {
    console.error("FAIL " + m + ": " + JSON.stringify(a) + " !== " + JSON.stringify(b));
    process.exit(1);
  }
}

// sseDelta: line -> content delta (or null)
eq(sseDelta('data: {"choices":[{"delta":{"content":"hi"}}]}'), "hi", "basic");
eq(sseDelta('data: {"choices":[{"delta":{"content":"x"}}]}\r'), "x", "trailing-CR-tolerated");
eq(sseDelta('data: [DONE]'), null, "done-sentinel");
eq(sseDelta(': keep-alive'), null, "comment-line");
eq(sseDelta('event: ping'), null, "non-data-line");
eq(sseDelta('data: {bad json'), null, "malformed-json");
eq(sseDelta('data: {"choices":[{"delta":{}}]}'), null, "no-content-field");
eq(sseDelta(''), null, "empty-line");

// sseLines: a line split across two feeds emerges once, whole (chunk boundary)
var feed = sseLines();
eq(feed('data: {"choices":[{"delta":{"content":"He'), [], "partial-held");
var out = feed('llo"}}]}\ndata: [DONE]\n');
eq(out.length, 2, "two-lines-after-boundary");
eq(sseDelta(out[0]), "Hello", "reassembled-delta");
eq(sseDelta(out[1]), null, "trailing-done");

// sseLines: multiple CRLF-terminated lines in one chunk; the CR rides through for
// sseDelta to strip, and a dangling partial (no newline) is withheld.
var f2 = sseLines();
var o2 = f2("data: a\r\ndata: b\r\ndata: c");
eq(o2, ["data: a\r", "data: b\r"], "crlf-split-holds-partial");
eq(sseDelta(o2[0]), null, "plain-a-not-json");

console.log("OK");
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_sse_parser_under_node():
    src = SHELL.read_text()
    driver = _fn(src, "sseLines") + "\n" + _fn(src, "sseDelta") + "\n" + _CHECKS
    r = subprocess.run([shutil.which("node"), "-e", driver],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, "node failed:\nSTDERR:\n" + r.stderr + "\nSTDOUT:\n" + r.stdout
    assert "OK" in r.stdout
