"""`benchstat.py contract-jev`: the Jev work-contract experiment's measures.

docs/design/agent-quality/jev-contract.md section 11, under the protocol of
docs/design/agent-quality/experiment-protocol.md section 8. The rules each test
holds, stated as the failure it prevents:

* the arm is READ BACK from every attempt's `result_summary.switches`, never
  taken from the assignment alone: an item whose record disagrees, or has no
  record, is excluded from both arms and listed, never relabelled;
* an unpriced arm prints no cost, never $0: Jev's tokens are what arm on adds,
  so a total that leaves them out would make the arm look free;
* JC1's and JC2's rates count only what ran, with what did not run apart;
* historical items with Jev on are reported apart (protocol section 5).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BENCHSTAT = ROOT / "scripts" / "benchstat.py"


def _load():
    spec = importlib.util.spec_from_file_location("benchstat", BENCHSTAT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bs = _load()

RATES = {"input": 1.0, "output": 5.0}


def _attempt(value, *, shape="record"):
    if value is None:
        return {"result_summary": {"switches": {}}}
    record = {"value": value} if shape == "record" else value
    return {"result_summary": {"switches": {"contract_jev": record}}}


def item(item_id, arm, *, recorded=None, accepted=True, review="ok", judgments=(),
         hidden="passed", verdict="MERGE", model=1.0, jev=(1000, 0), submitted=0,
         latency=60.0, approvals=1, overrides=0, historical=False, **extra):
    recorded = arm if recorded is None else recorded
    out = {
        "item_id": item_id, "arm": arm,
        "attempts": [_attempt(recorded)],
        "review": None if review is None else {"outcome": review, **extra.pop("review_extra", {})},
        "judgments": [dict(j) for j in judgments],
        "hidden_tests": hidden, "review_verdict": verdict, "accepted": accepted,
        "model_cost_usd": model,
        "jev_tokens": None if jev is None else {"input": jev[0], "output": jev[1]},
        "submitted_at": submitted,
        "accepted_at": None if latency is None else submitted + latency,
        "approvals": approvals, "owner_overrides": overrides, "historical": historical,
    }
    out.update(extra)
    return out


def off(item_id, **kw):
    kw.setdefault("review", "skipped:arm")
    kw.setdefault("jev", (0, 0))
    return item(item_id, "off", **kw)


# --- the arm is read back --------------------------------------------------------


def test_the_switch_name_is_the_one_swarm_api_accepts():
    from swarm_api.validation import DISPATCH_SWITCHES, dispatch_switch_values

    assert bs.CONTRACT_JEV_SWITCH in DISPATCH_SWITCHES
    assert tuple(bs.CONTRACT_JEV_ARMS) == dispatch_switch_values(bs.CONTRACT_JEV_SWITCH)


@pytest.mark.parametrize("shape", ["record", "word"])
def test_the_arm_reads_back_from_result_summary_switches(shape):
    a = item("a", "on")
    a["attempts"] = [_attempt("on", shape=shape), _attempt("on", shape=shape)]
    table = bs.contract_jev_table([a, off("b")], jev_token_rates=RATES)
    assert table["arms"]["on"]["items"] == 1
    assert table["arms"]["off"]["items"] == 1
    assert table["excluded"] == []


def test_an_attempt_whose_record_disagrees_is_an_arm_mismatch_never_relabelled():
    a = item("a", "on")
    a["attempts"].append(_attempt("off"))
    table = bs.contract_jev_table([a, off("b")], jev_token_rates=RATES)
    assert table["arms"]["on"]["items"] == 0
    assert table["arms"]["off"]["items"] == 1
    assert table["excluded"] == [{"item_id": "a", "arm": "on", "reason": "arm_mismatch",
                                  "recorded": ["off", "on"]}]


def test_an_attempt_with_no_record_is_excluded_not_counted_in_its_assigned_arm():
    # Missing verification is not a pass: an attempt that does not say its
    # arm cannot prove it ran in it.
    table = bs.contract_jev_table([item("a", "on", recorded=None) | {"attempts": [_attempt(None)]}],
                                  jev_token_rates=RATES)
    assert table["arms"]["on"]["items"] == 0
    assert table["excluded"][0]["reason"] == "arm_unrecorded"


@pytest.mark.parametrize("arm", ["off", "on"])
def test_an_item_recorded_by_the_worker_and_swarm_api_is_counted_in_its_arm(tmp_path, arm):
    # End to end with what the platform records: the attempt's switches as
    # `graphstage.switches_summary` writes them, and under arm off the
    # review's outcome as `contractreview` records it and JC2's posted reason
    # as the worker's post-step sends it. Before both read `contract_jev`,
    # every such item was excluded as `arm_unrecorded`.
    from agent_worker import graphstage
    from agent_worker.judge import poststeps
    from swarm_api import contractreview

    switches = graphstage.switches_summary(tmp_path, {"contract_jev": arm})
    assert switches is not None
    attempt = {"result_summary": {graphstage.SWITCHES_FIELD: switches}}
    assert bs.recorded_arm(attempt) == arm
    if arm == "off":
        assert contractreview.SKIPPED_ARM == poststeps.SKIPPED_ARM == bs.SKIPPED_ARM
        a = off("a", review=contractreview.SKIPPED_ARM)
    else:
        a = item("a", "on")
    a["attempts"] = [attempt, attempt]
    table = bs.contract_jev_table([a], jev_token_rates=RATES)
    assert table["excluded"] == []
    assert table["arms"][arm]["items"] == 1


def test_an_item_with_no_attempts_is_excluded():
    a = item("a", "on")
    a["attempts"] = []
    assert bs.contract_jev_table([a])["excluded"][0]["reason"] == "arm_unrecorded"


def test_an_unassigned_arm_is_excluded():
    a = item("a", "maybe")
    assert bs.contract_jev_table([a])["excluded"][0]["reason"] == "arm_unassigned"


@pytest.mark.parametrize("leak", [
    {"review": "refused"},
    {"judgments": [{"verdict": "met", "p": 0.9}]},
])
def test_jev_running_in_arm_off_is_a_mismatch(leak):
    # Arm off is exactly today's behaviour: JC1 and JC2 skipped as
    # `skipped:arm`. An off item Jev reviewed or judged did not run in arm off.
    table = bs.contract_jev_table([off("b", **leak)], jev_token_rates=RATES)
    assert table["arms"]["off"]["items"] == 0
    assert table["excluded"][0]["reason"] == "arm_mismatch"


def test_jev_skipped_by_the_arm_in_arm_on_is_a_mismatch():
    table = bs.contract_jev_table([item("a", "on", review="skipped:arm")], jev_token_rates=RATES)
    assert table["excluded"][0]["reason"] == "arm_mismatch"


# --- cost: an unpriced arm prints no cost ----------------------------------------


def test_an_unpriced_arm_prints_no_cost_never_zero():
    table = bs.contract_jev_table([item("a", "on"), off("b")], jev_token_rates=None)
    on = table["arms"]["on"]["cost"]
    assert on["status"] == "unpriced"
    assert on["total_usd"] is None and on["per_accepted_usd"] is None
    text = bs.render_contract_jev(table)
    on_line = next(line for line in text.splitlines() if line.startswith("on "))
    assert "$" not in on_line and "unpriced" in on_line
    assert "$0" not in text.replace("$0.", "")


def test_an_empty_rate_setting_is_unpriced_too():
    table = bs.contract_jev_table([item("a", "on")], jev_token_rates={})
    assert table["arms"]["on"]["cost"]["status"] == "unpriced"


def test_an_arm_that_spent_no_jev_tokens_is_priced_without_the_rates():
    table = bs.contract_jev_table([off("b", model=2.5)], jev_token_rates=None)
    cost = table["arms"]["off"]["cost"]
    assert cost["status"] == "priced" and cost["total_usd"] == pytest.approx(2.5)


def test_a_priced_arm_sums_model_cost_and_jev_tokens_at_the_rates():
    items = [item("a", "on", model=1.0, jev=(1_000_000, 200_000)),
             item("c", "on", model=2.0, jev=(500_000, 0), accepted=False)]
    cost = bs.contract_jev_table(items, jev_token_rates=RATES)["arms"]["on"]["cost"]
    # 1.0 + 2.0 model, 1.5M input at $1 and 0.2M output at $5.
    assert cost["status"] == "priced"
    assert cost["total_usd"] == pytest.approx(3.0 + 1.5 + 1.0)
    # Failed items' cost counts; the denominator is the accepted items.
    assert cost["per_accepted_usd"] == pytest.approx(5.5)


def test_a_missing_model_cost_makes_the_total_a_floor_with_its_coverage():
    items = [item("a", "on", model=None), item("c", "on", model=2.0)]
    cost = bs.contract_jev_table(items, jev_token_rates=RATES)["arms"]["on"]["cost"]
    assert cost["status"] == "at_least"
    assert cost["coverage"] == pytest.approx(0.5)
    assert "at least" in bs.render_contract_jev(
        bs.contract_jev_table(items, jev_token_rates=RATES))


def test_unknown_jev_tokens_are_not_zero_tokens():
    cost = bs.contract_jev_table([item("a", "on", jev=None)],
                                 jev_token_rates=RATES)["arms"]["on"]["cost"]
    assert cost["status"] == "at_least" and cost["coverage"] == 0.0


def test_an_arm_with_no_accepted_item_has_no_cost_per_accepted_item():
    cost = bs.contract_jev_table([item("a", "on", accepted=False)],
                                 jev_token_rates=RATES)["arms"]["on"]["cost"]
    assert cost["total_usd"] is not None and cost["per_accepted_usd"] is None


def test_the_rates_are_read_as_the_platform_setting_reads_them():
    from swarm_api.jevusage import rates_of

    for value in ({"input": 1, "output": 2}, {"input": -1}, {"input": True}, {}, None,
                  {"input": float("inf"), "output": 0}):
        assert bs.jev_rates_of(value) == rates_of(value)


# --- outcome --------------------------------------------------------------------


def test_the_success_rate_is_wilson_and_matches_swarm_api():
    from swarm_api.outcomes import wilson

    items = [item(f"a{i}", "on", accepted=i < 7) for i in range(10)]
    rate = bs.contract_jev_table(items, jev_token_rates=RATES)["arms"]["on"]["accepted"]
    assert rate == wilson(7, 10)
    for k, n in ((0, 0), (0, 5), (5, 5), (272, 300)):
        assert bs.wilson(k, n) == wilson(k, n)


def test_only_true_is_accepted():
    items = [item("a", "on", accepted=None), item("b", "on", accepted="yes")]
    assert bs.contract_jev_table(items)["arms"]["on"]["accepted"]["k"] == 0


def test_latency_is_nearest_rank_over_accepted_items_with_the_unended_counted():
    items = [item(f"a{i}", "on", latency=float(10 * (i + 1))) for i in range(10)]
    items.append(item("x", "on", latency=None))
    items.append(item("y", "on", accepted=False, latency=999.0))
    latency = bs.contract_jev_table(items, jev_token_rates=RATES)["arms"]["on"]["latency_s"]
    assert latency["p50"] == 50.0 and latency["p95"] == 100.0
    assert latency["n"] == 10 and latency["no_end"] == 1
    assert "mean" not in latency


def test_owner_time_per_accepted_item_and_overrides():
    items = [item("a", "on", approvals=2, overrides=1), item("b", "on", approvals=1),
             item("c", "on", accepted=False, approvals=None)]
    owner = bs.contract_jev_table(items, jev_token_rates=RATES)["arms"]["on"]["owner"]
    assert owner["approvals_per_accepted"] == pytest.approx(1.5)
    assert owner["overrides"] == 1
    assert owner["unknown"] == 1


# --- JC1 ---------------------------------------------------------------------------


def test_jc1_rates_count_the_reviews_that_ran_and_list_the_rest():
    items = [item("a", "on", review="refused", review_extra={"resubmitted": True}),
             item("b", "on", review="refused", review_extra={
                 "overridden": True, "override_judged_cleanly": True}),
             item("c", "on", review="amended"),
             item("d", "on", review="ok"),
             item("e", "on", review="not_run", review_extra={"reason": "http_503"}),
             item("f", "on", review=None)]
    jc1 = bs.contract_jev_table(items, jev_token_rates=RATES)["arms"]["on"]["jc1"]
    assert jc1["refused"]["k"] == 2 and jc1["refused"]["n"] == 4
    assert jc1["amended"]["k"] == 1 and jc1["amended"]["n"] == 4
    assert jc1["not_run"] == {"http_503": 1}
    assert jc1["unrecorded"] == 1
    assert jc1["resubmitted"] == 1 and jc1["overridden"] == 1
    assert jc1["false_refusals"] == 1


# --- JC2 ---------------------------------------------------------------------------


def test_jc2_false_send_back_missed_defect_and_cannot_judge():
    items = [
        # unmet, hidden tests pass, MERGE: a false send-back.
        item("a", "on", judgments=[{"verdict": "unmet", "p": 0.8}]),
        # unmet, but the review said REVISE: not a false send-back.
        item("b", "on", verdict="REVISE", judgments=[{"verdict": "unmet", "p": 0.8}]),
        # met, hidden tests fail: a missed defect.
        item("c", "on", hidden="failed", accepted=False,
             judgments=[{"verdict": "met", "p": 0.9}]),
        item("d", "on", judgments=[{"verdict": "met", "p": 0.9},
                                   {"verdict": "cannot_judge", "p": 0.5}]),
    ]
    jc2 = bs.contract_jev_table(items, jev_token_rates=RATES)["arms"]["on"]["jc2"]
    assert jc2["judgments"] == 5
    assert jc2["false_send_back"]["k"] == 1 and jc2["false_send_back"]["n"] == 2
    assert jc2["missed_defect"]["k"] == 1 and jc2["missed_defect"]["n"] == 2
    assert jc2["cannot_judge"]["k"] == 1 and jc2["cannot_judge"]["n"] == 5


def test_jc2_brier_reads_p_as_the_probability_of_the_stated_verdict():
    items = [item("a", "on", judgments=[{"verdict": "met", "p": 0.9}]),
             item("b", "on", hidden="failed", accepted=False,
                  judgments=[{"verdict": "unmet", "p": 0.8}])]
    jc2 = bs.contract_jev_table(items, jev_token_rates=RATES)["arms"]["on"]["jc2"]
    # (0.9 - 1)^2 and ((1 - 0.8) - 0)^2.
    assert jc2["brier"] == pytest.approx((0.01 + 0.04) / 2)
    assert jc2["brier_n"] == 2


def test_a_hidden_test_that_did_not_run_scores_nothing():
    items = [item("a", "on", hidden="not_run", accepted=False,
                  judgments=[{"verdict": "met", "p": 0.9}])]
    jc2 = bs.contract_jev_table(items, jev_token_rates=RATES)["arms"]["on"]["jc2"]
    assert jc2["brier"] is None and jc2["brier_n"] == 0
    assert jc2["missed_defect"] is None
    assert jc2["no_hidden_result"] == 1


# --- historical items --------------------------------------------------------------


def test_historical_items_with_jev_on_are_reported_apart():
    items = [item("a", "on"), item("h", "on", historical=True), off("o", historical=True)]
    arms = bs.contract_jev_table(items, jev_token_rates=RATES)["arms"]
    assert arms["on"]["items"] == 1
    assert arms["on:historical"]["items"] == 1
    assert arms["off"]["items"] == 1


# --- the command -------------------------------------------------------------------


def test_the_command_reads_jsonl_and_prints_no_cost_for_an_unpriced_arm(tmp_path, capsys):
    path = tmp_path / "items.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in (item("a", "on"), off("b"))) + "\n\n")
    assert bs.main(["contract-jev", "--input", str(path)]) == 0
    text = capsys.readouterr().out
    on_line = next(line for line in text.splitlines() if line.startswith("on "))
    assert "unpriced" in on_line and "$" not in on_line

    assert bs.main(["contract-jev", "--input", str(path), "--json",
                    "--jev-token-rates", json.dumps(RATES)]) == 0
    table = json.loads(capsys.readouterr().out)
    assert table["arms"]["on"]["cost"]["status"] == "priced"
    assert table["jev_token_rates"] == RATES


def test_a_rate_setting_that_is_not_the_platform_shape_is_refused(tmp_path):
    path = tmp_path / "items.jsonl"
    path.write_text("")
    with pytest.raises(SystemExit):
        bs.main(["contract-jev", "--input", str(path), "--jev-token-rates", '{"in": 1}'])


def test_the_self_test_still_passes():
    assert bs.main(["self-test"]) == 0
