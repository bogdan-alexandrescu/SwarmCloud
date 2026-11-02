"""`sc capacity`'s Jev usage block: the console's Capacity › Jev usage, in the terminal.

The owner asked on 2 November 2026 for the same Jev usage numbers in the
plugin's capacity view (part of #2688). The block reads `GET /v1/jev-usage`,
the route the console's page reads, and words what is absent exactly as the
page does: dollars without a rate are "rate not set", never $0; calls no row
split by kind are "not split by kind"; outcomes no row recorded are "not
recorded", never "0 ok". An older API that does not serve the route is one
line, and the capacity view above it still renders and still exits 0.

Offline: the control plane is a dict, as in test_sc.py. Every figure below is
invented.
"""

from __future__ import annotations

import io

import pytest

from swarm_mcp import render, sc, server
from swarm_mcp.client import SwarmError

CAPACITY = {
    "tenant_id": "example-ws",
    "pools": [{"name": "global", "enabled": True, "effective_limit": 40, "hard_limit": 40,
               "active": 3, "available": 37}],
    "runner_profiles": {},
}

#: An answer in /v1/jev-usage's shape (`swarm_api.jevusage.summarise`).
PRICED = {
    "tenant_id": "example-ws",
    "window": {"from": "2026-10-04", "to": "2026-11-02", "days": 30},
    "rate": {"input_usd_per_million": 3.0, "output_usd_per_million": 15.0, "set": True},
    "days": [],
    "total": {
        "calls": 1234,
        "input_tokens": 5678900,
        "output_tokens": 43210,
        "outcomes": {"ok": 1100, "failed": 7, "skipped": 27,
                     "skipped_by_reason": {"budget_calls": 20, "oversize": 7},
                     "unrecorded": 100},
        "usd": 17.68485,
        "by_kind": [
            {"kind": "J1", "group": "worker_judgment", "calls": 1000, "input_tokens": 5000000,
             "output_tokens": 43210,
             "outcomes": {"ok": 966, "failed": 7, "skipped": 27,
                          "skipped_by_reason": {"budget_calls": 20, "oversize": 7},
                          "unrecorded": 0},
             "usd": 15.64815},
            {"kind": "browser", "group": "jev_browser", "calls": 134, "input_tokens": 578900,
             "output_tokens": None,
             "outcomes": {"ok": 134, "failed": 0, "skipped": 0, "skipped_by_reason": {},
                          "unrecorded": 0},
             "usd": 1.7367},
            {"kind": "unattributed", "group": "unattributed", "calls": 100, "input_tokens": 100000,
             "output_tokens": None, "outcomes": None, "usd": 0.3},
        ],
    },
    "undated": None,
    "month": {"month": "2026-11", "used_tokens": 250000, "cap_tokens": 1000000,
              "used_fraction": 0.25},
}

#: The same workspace before a platform admin set Jev's per-token rate.
UNPRICED = {
    **PRICED,
    "rate": {"input_usd_per_million": None, "output_usd_per_million": None, "set": False},
    "total": {
        "calls": 100, "input_tokens": 100000, "output_tokens": None, "outcomes": None,
        "usd": None,
        "by_kind": [{"kind": "unattributed", "group": "unattributed", "calls": 100,
                     "input_tokens": 100000, "output_tokens": None, "outcomes": None,
                     "usd": None}],
    },
    "month": {"month": "2026-11", "used_tokens": 100000, "cap_tokens": None,
              "used_fraction": None},
}


class FakeClient:
    tier = "explicit"
    base_url = "https://swarm-api.example.test"

    def __init__(self, routes):
        self.routes = routes
        self.asked: list[str] = []

    def request(self, method, path, **kwargs):
        self.asked.append(path)
        answer = self.routes.get(path.split("?")[0])
        if answer is None:
            raise SwarmError(f"{method} {path} -> 500: no such route", status=500)
        if isinstance(answer, Exception):
            raise answer
        return answer


def capacity_screen(jev):
    client = FakeClient({"/v1/capacity": CAPACITY, "/v1/jev-usage": jev})
    out = io.StringIO()
    args = sc.build_parser().parse_args(["--no-color", "--width", "100", "capacity"])
    code = sc.cmd_capacity(client, args, out)
    return code, out.getvalue(), client


def text(usage, **kw):
    return "\n".join(render.render_jev_usage(usage, render.Style(width=100), **kw))


class TestAFixtureAnswer:
    def test_sc_capacity_reads_the_route_the_console_reads(self):
        code, out, client = capacity_screen(PRICED)
        assert code == sc.EXIT_OK
        assert "/v1/jev-usage" in client.asked
        assert "JEV USAGE" in out
        assert "CAPACITY" in out and "global" in out

    def test_the_30_day_calls_tokens_and_dollars(self):
        out = text(PRICED)
        assert "last 30 days" in out
        assert "1,234" in out
        assert "5,678,900" in out
        assert "43,210" in out
        assert "$17.68" in out

    def test_the_outcomes_and_every_skip_reason_as_the_console_words_them(self):
        out = text(PRICED)
        assert ("1,100 ok · 7 failed · 27 skipped (call budget spent 20, "
                "question too large 7) · 100 not recorded") in out

    def test_each_kind_is_named_as_on_the_console(self):
        out = text(PRICED)
        assert "Worker judgment J1" in out
        assert "Jev browser" in out
        assert "not split by kind" in out

    def test_a_kind_with_no_outcomes_says_not_recorded_never_zero(self):
        lines = [line for line in text(PRICED).splitlines() if "not split by kind" in line]
        assert lines and all("not recorded" in line for line in lines)
        assert all("0 ok" not in line for line in lines)

    def test_output_tokens_no_row_recorded_are_not_recorded(self):
        browser = [line for line in text(PRICED).splitlines() if "Jev browser" in line]
        assert browser and "not recorded" in browser[0]

    def test_the_monthly_caps_use(self):
        out = text(PRICED)
        assert "2026-11" in out
        assert "250,000 of 1,000,000 tokens" in out
        assert "25%" in out

    def test_the_rate_it_was_priced_at(self):
        assert "$3 per million tokens in, $15 out" in text(PRICED)

    def test_the_bridge_tool_draws_the_same_block(self):
        client = FakeClient({"/v1/capacity": CAPACITY, "/v1/jev-usage": PRICED})
        out = server._call(client, "swarm_capacity", {"width": 80})
        assert "JEV USAGE" in out and "1,234" in out
        for line in out.splitlines():
            assert len(line) <= 80, line


class TestNoRateSet:
    def test_dollars_read_rate_not_set_never_zero(self):
        out = text(UNPRICED)
        assert "rate not set" in out
        assert "$0" not in out

    def test_outcomes_never_recorded_read_not_recorded(self):
        out = text(UNPRICED)
        facts = [line for line in out.splitlines() if line.strip().startswith("outcomes")]
        assert facts and "not recorded" in facts[0]
        assert "0 ok" not in out

    def test_no_cap_says_so_with_the_months_tokens(self):
        out = text(UNPRICED)
        assert "100,000 tokens in this month" in out
        assert "no monthly cap is set" in out

    def test_through_sc_capacity(self):
        code, out, _ = capacity_screen(UNPRICED)
        assert code == sc.EXIT_OK
        assert "rate not set" in out and "$0" not in out


class TestTheRouteMissing:
    @pytest.mark.parametrize("status", [403, 404])
    def test_an_older_api_is_one_line_and_the_capacity_view_still_renders(self, status):
        refused = SwarmError(f"GET /v1/jev-usage -> {status}: Not Found", status=status)
        code, out, _ = capacity_screen(refused)
        assert code == sc.EXIT_OK, "an absent Jev usage route is not a failed capacity view"
        assert "global" in out and "37" in out
        jev = out[out.index("JEV USAGE"):].strip().splitlines()
        assert len(jev) == 2, jev
        assert "does not serve /v1/jev-usage" in jev[1]

    def test_a_route_that_fails_is_said_and_the_capacity_view_still_renders(self):
        code, out, _ = capacity_screen(SwarmError("GET /v1/jev-usage -> 500: boom", status=500))
        assert code == sc.EXIT_OK
        assert "global" in out
        assert "Jev usage could not be read" in out and "500" in out

    def test_the_json_dump_carries_the_answer_and_the_absence(self):
        client = FakeClient({"/v1/capacity": CAPACITY,
                             "/v1/jev-usage": SwarmError("-> 404", status=404)})
        out = io.StringIO()
        sc.cmd_capacity(client, sc.build_parser().parse_args(["--json", "capacity"]), out)
        assert '"jev_usage": null' in out.getvalue()
        assert '"jev_usage_absent": true' in out.getvalue()
