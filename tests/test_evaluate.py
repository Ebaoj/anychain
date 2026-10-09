"""PHASE3 T5 (R7, D50): the evaluation's measurements, without calling a model."""
import json

import yaml
from typer.testing import CliRunner

from anychain import cli
from anychain.evaluate import ROOT, factual_sentences, load_cases, run_case, summarize

CASES = ROOT / "eval" / "cases.yaml"


def test_the_set_covers_the_plans_categories_with_real_recordings():
    cases = load_cases(CASES)
    ids = {c["id"] for c in cases}
    assert {"erc20_transfer", "dex_swap", "explicit_reason", "out_of_gas", "unverified_contract",
            "custom_error", "node_unavailable"} <= ids
    # the allowance category has a real case only on the private demo network (D60), never filled with another one
    assert "allowance_devnet" in ids and not (yaml.safe_load(CASES.read_text()).get("missing") or [])
    assert sum(c["config"] == "ethereum-mainnet" for c in cases) >= 8
    assert sum(c["config"] != "ethereum-mainnet" for c in cases) >= 2
    for c in cases:
        assert (ROOT / "tests" / "fixtures" / f"{c['fixture']}.json").exists(), c["id"]


def test_every_case_meets_its_expectations_on_the_evidence():
    for case in load_cases(CASES):
        r = run_case(case, write=False)
        assert (r.status_ok, r.rule_ok, r.label_ok, r.abi_ok, r.degradation_ok in (None, True)) == \
            (True, True, True, True, True), (case["id"], r.got)


def test_a_citation_after_the_full_stop_counts_and_headings_do_not():
    text = ("**What happened:** the transaction failed and nothing moved. [E1]\n\n**Next steps:**\n"
            "- Check the balance before trying again [E3].\n- Try again with a smaller amount next time.\nWhy?")
    sentences = factual_sentences(text)
    assert len(sentences) == 3
    assert [("[E" in s) for s in sentences] == [True, True, False]


class Scripted:
    def __init__(self, *answers):
        self.answers, self.last_usage = list(answers), {"input_tokens": 3, "output_tokens": 50,
                                                         "cache_read_tokens": 0, "cache_write_tokens": 0,
                                                         "cost_usd": 0.01}

    def complete(self, system, user):
        return self.answers.pop(0)


def test_a_written_case_is_measured():
    case = next(c for c in load_cases(CASES) if c["id"] == "out_of_gas")
    # it cites the conclusion (E6) and the timeline's pattern (E9): an answer without them is rewritten (D62)
    r = run_case(case, write=True, backend=Scripted("It ran out of gas: 60000 of 60000 were used [E1] [E6]. The fee "
                                                    "was charged anyway in this transaction [E2]. The same transfer "
                                                    "failed several times in a row [E9]."))
    assert r.summary_status == "ok" and (r.cited, r.sentences) == (3, 3) and r.entities_found == [True]
    assert r.key_facts == ["E6", "E9"] and r.key_facts_cited == ["E6", "E9"]
    s = summarize([r])
    assert s["citation_coverage"] == 100.0 and s["hallucination_rate"] == 0.0 and s["cost_usd"] == 0.01


def test_a_first_attempt_with_an_invented_value_is_counted():
    case = next(c for c in load_cases(CASES) if c["id"] == "out_of_gas")
    r = run_case(case, write=True, backend=Scripted("It used 99999 gas [E1].", "It ran out of gas, using all 60000 [E1]."))
    assert r.summary_status == "retried" and r.first_attempt_problems
    assert summarize([r])["hallucination_rate"] == 100.0


def test_the_command_writes_the_report(tmp_path):
    cases = tmp_path / "cases.yaml"
    cases.write_text(yaml.safe_dump({"cases": [c for c in load_cases(CASES) if c["id"] == "node_unavailable"]}))
    result = CliRunner().invoke(cli.app, ["eval", "--cases", str(cases), "--no-llm", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["summary"]["degradation_declared"] == 100.0 and report["commit"]
    assert "| node_unavailable |" in (tmp_path / "report.md").read_text()
