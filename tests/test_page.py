"""PHASE3 T4 (R2, D48): the page reads only fields the API gives, and escapes everything it shows.

The second test runs the page's own rendering functions in Node (skipped when Node is not installed) on real
answers whose every text has been replaced by markup meant to break out.
"""
import json
import re
import shutil
import subprocess

import pytest

from anychain.answer import structured_answer
from anychain.api import PAGE
from anychain.config import load_config
from tests.conftest import ROOT, replay_bundle
from tests.test_golden import _case

CASES = [("celo_fail_balance_confirmed", "celo-mainnet", None),  # a diagnosis with reads
         ("celo_fail_balance_rpc_only", "celo-mainnet", {"celo.blockscout.com"}),  # gaps, a replay LIKELY
         ("eth_swaprouter02_multicall", "ethereum-mainnet", None)]  # security notes, transfers, code


def _answers():
    for fixture, config, offline in CASES:
        _c, tx = _case(fixture)
        b = replay_bundle(load_config(str(ROOT / "configs" / f"{config}.yaml")), tx, fixture, offline_hosts=offline)
        yield {**structured_answer(b, "It **failed** [E1].", "ok", "support"), "run_id": 7}


def test_every_field_the_page_reads_is_in_real_answers():
    seen = {"diagnosis": False, "gaps": False, "security_notes": False, "transfers": False}
    for a in _answers():
        for key in ("status", "tx_hash", "confidence", "summary", "summary_status", "run_id", "diagnosis", "next_steps",
                    "mode", "security_notes", "gaps", "transfers", "sources", "evidence"):
            assert key in a, key
        assert "status_basis" in a["confidence"]
        for d in a["diagnosis"]:
            assert {"label", "text", "facts"} <= set(d)
        for n in a["security_notes"]:
            assert all("text" in x for x in n["notes"])
        for g in a["gaps"]:
            assert {"what", "why", "needed", "retryable"} <= set(g)
        for s in a["sources"]:
            assert {"url", "label", "facts", "kind"} <= set(s)
        for e in a["evidence"]:
            assert {"id", "text", "kind", "confidence", "sources"} <= set(e)
        for key in seen:
            seen[key] = seen[key] or bool(a[key])
    assert all(seen.values()), seen  # the three answers cover every part of the page


ATTACK = '<img src=x onerror=alert(1)>"\' onclick="alert(2)" [E1"><svg onload=alert(3)>] `<b>` **<i>**'


def _hostile(value):
    if isinstance(value, dict):
        return {k: _hostile(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_hostile(v) for v in value]
    return ATTACK if isinstance(value, str) else value


HARNESS = r"""
const page = require("fs").readFileSync(process.argv[2], "utf8");
const script = page.split("<script>")[1].split("</script>")[0];
const cut = (from, to) => script.slice(script.indexOf(from), script.indexOf(to));
const elements = {};
const $ = (id) => elements[id] || (elements[id] = {innerHTML: "", classList: {add() {}, remove() {}}});
const state = {evidence: new Map()};
const code = cut("function esc(", "async function api(") + cut("function renderAnswer(", "// ---- end of the rendering functions ----");
const render = new Function("$", "state", code + "; return renderAnswer;")($, state);
const answers = JSON.parse(require("fs").readFileSync(0, "utf8"));
for (const a of answers) render(a);
console.log(JSON.stringify(Object.values(elements).map((e) => e.innerHTML).join("\n")));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_the_page_escapes_every_text_it_shows(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)
    answers = [_hostile(a) for a in _answers()]
    for a in answers:  # keep the shapes the page branches on
        a["status"], a["mode"], a["summary_status"] = "failed", "support", "ok"
        a["run_id"] = 7
    done = subprocess.run(["node", str(harness), str(PAGE)], input=json.dumps(answers), capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    html = json.loads(done.stdout)
    assert ATTACK.split(">")[0] not in html  # no raw <img
    for raw in ("<img", "<svg", "<b>", "<i>", 'onclick="alert', "onerror=alert(1)>"):
        assert raw not in html, raw
    assert len(re.findall(r"&lt;img src=x onerror=alert\(1\)&gt;", html)) > 10  # the texts are there, escaped
