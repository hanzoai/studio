"""Quality gate — the verdict must survive REASONING judges.

zen-vl became a reasoning model: with a starved max_tokens the whole budget went
to reasoning_content, content came back null, the strict parse saw nothing and
the gate failed open on EVERY render (flagged count reached zero while corrupted
outputs sat in the grid). These pin the fixed parsing."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from middleware import quality_gate as qg


def _completion(content=None, reasoning=None):
    msg = {"role": "assistant", "content": content}
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    return {"choices": [{"message": msg}]}


def test_reply_reads_plain_content():
    assert qg._reply_text(_completion(content="FAIL — mottled skin")) == "FAIL — mottled skin"


def test_reply_reads_parts_array():
    assert qg._reply_text(_completion(content=[{"type": "text", "text": "PASS"}])) == "PASS"


def test_reply_falls_back_to_reasoning_content():
    got = qg._reply_text(_completion(content=None, reasoning="the skin shows mosaic damage. FAIL"))
    assert "FAIL" in got


def test_reply_empty_when_nothing():
    assert qg._reply_text(_completion(content=None)) == ""


def test_verdict_from_reasoning_reply_flags():
    reply = qg._reply_text(_completion(content=None, reasoning="Looking closely... verdict: FAIL"))
    assert qg.parse_verdict(reply) == "fail"


def test_judge_payload_not_starved():
    import inspect
    src = inspect.getsource(qg.judge)
    assert '"max_tokens": 8,' not in src


def test_verdict_last_token_wins_over_deliberation():
    assert qg.parse_verdict("I must answer PASS or FAIL. The skin is mottled: FAIL") == "fail"
    assert qg.parse_verdict("Could be a FAIL, but the artifacts are compression. PASS") == "pass"
    assert qg.parse_verdict("no verdict words here") == "inconclusive"
