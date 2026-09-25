"""What `sc` puts on a terminal, checked without a terminal or a cluster.

Offline by construction, and not merely as a convenience. The two behaviours
worth the most here are the ones a live system will not produce on demand:

  * a STALE reading. `stale` means "no measurement has arrived recently enough
    to trust", which you cannot ask a healthy broker to be. If this rule were
    tested against a real deployment it would be tested by hand, once, and
    then never again -- so `render.py` takes already-fetched data and the
    fixtures below simply say `stale: True`.

  * a NARROW terminal. The screen an operator has at 3am is often an 80-column
    ssh session inside a split pane, and a table that wraps there is a table
    nobody can read. Width is a parameter, so every width is testable.

THE RULE THESE TESTS EXIST TO PIN: a stale or absent reading is never printed
as a bare number, and "unknown" is never printed as zero. Those two claims --
"this account has used none of its quota" and "nobody has asked" -- are
different, and a renderer that collapses them makes a dead poller look like a
healthy pool. Nearly every assertion below is some form of that one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_common.states import CONCURRENCY_STATES, PENDING_STATES
from swarm_mcp import render
from swarm_mcp.render import (
    Binding,
    Cell,
    Column,
    Finding,
    Reading,
    Snapshot,
    Style,
    account_is_stale,
    binding_pool,
    find_trouble,
    format_percent,
    format_until,
    format_window,
    public_account,
    read_window,
    render_accounts,
    render_agents,
    render_capacity,
    render_overview,
    render_table,
    render_task,
    render_trouble,
    window_keys,
)

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
WIDE = Style(width=80, color=False, unicode=True)
ASCII = Style(width=80, color=False, unicode=False)
COLOUR = Style(width=80, color=True, unicode=True)


def iso(**delta) -> str:
    return (NOW + timedelta(**delta)).isoformat()


def account(
    account_id="acme:main",
    *,
    five=0.12,
    seven=0.40,
    stale=False,
    state="AVAILABLE",
    reason="",
    observed=True,
    windows=None,
    **extra,
):
    """One account in the exact shape `account_to_api` documents."""
    if windows is None:
        windows = {}
        if five is not None:
            windows["five_hour"] = {
                "utilization": five, "resets_at": iso(hours=3), "reset": False,
            }
        if seven is not None:
            windows["seven_day"] = {
                "utilization": seven, "resets_at": iso(days=2), "reset": False,
            }
    payload = {
        "account_id": account_id,
        "owner_tenant": account_id.split(":")[0],
        "label": account_id.split(":")[-1],
        "provider": "anthropic",
        "state": state,
        "reason": reason,
        "lend_to": [],
        "assigned": 0,
        "windows": windows,
        "observed_at": iso(minutes=-2) if observed else None,
        "stale": stale,
    }
    payload.update(extra)
    return payload


def pool(name, *, limit=10, active=0, enabled=True, **extra):
    payload = {
        "name": name,
        "hard_limit": limit,
        "adaptive_target": None,
        "quota_derived_limit": None,
        "effective_limit": limit,
        "active": active,
        "available": max(0, limit - active),
        "enabled": enabled,
        "updated_at": iso(),
    }
    payload.update(extra)
    return payload


def text(lines) -> str:
    return "\n".join(lines)


# ==========================================================================
# The three marks
# ==========================================================================


class TestMarks:
    def test_a_fresh_reading_is_a_bare_number(self):
        assert format_percent(read_window(account(five=0.12), "five_hour"), WIDE) == "12%"

    def test_a_stale_reading_is_never_a_bare_number(self):
        reading = read_window(account(five=0.12, stale=True), "five_hour")
        rendered = format_percent(reading, WIDE)
        assert rendered == "~12%"
        assert rendered != "12%"

    def test_an_absent_reading_is_an_em_dash_and_not_a_zero(self):
        # The whole rule in one assertion: no window at all must not read as
        # "this account has used none of its quota".
        reading = read_window(account(windows={}), "five_hour")
        assert not reading.known
        rendered = format_percent(reading, WIDE)
        assert rendered == WIDE.dash
        assert "0" not in rendered
        assert "%" not in rendered

    def test_a_measured_zero_survives_as_zero(self):
        # The converse, and the reason absence is tested with `is None` rather
        # than truthiness: 0.0 is a measurement and must not vanish.
        reading = read_window(account(five=0.0), "five_hour")
        assert reading.known
        assert format_percent(reading, WIDE) == "0%"

    def test_a_reset_window_is_a_projection_even_when_freshly_read(self):
        # `reset` means the window this number describes is over. The reading
        # is minutes old and still cannot be quoted as current.
        acct = account(windows={
            "five_hour": {"utilization": 0.9, "resets_at": iso(hours=-1), "reset": True}
        })
        reading = read_window(acct, "five_hour")
        assert reading.known and not reading.stale
        assert reading.trusted is False
        assert format_percent(reading, WIDE) == "~90%"

    def test_a_malformed_utilisation_is_unknown_not_zero(self):
        acct = account(windows={"five_hour": {"utilization": "banana", "reset": False}})
        assert format_percent(read_window(acct, "five_hour"), WIDE) == WIDE.dash

    def test_ascii_style_swaps_every_glyph(self):
        assert ASCII.dash == "--"
        rendered = format_window(read_window(account(five=0.5), "five_hour"), ASCII).text
        rendered.encode("ascii")  # raises if a bar glyph leaked through
        assert "#" in rendered


class TestStaleness:
    def test_absent_provenance_is_stale_provenance(self):
        # No `observed_at` means nothing has ever read this account. That is
        # the strongest possible reason not to present its figures as current.
        assert account_is_stale(account(observed=False)) is True

    def test_a_payload_that_omits_stale_is_treated_as_stale(self):
        # `account.get("stale", False)` would be the Python spelling of the
        # `false // true` trap: it grants trust the payload never claimed.
        bare = account()
        bare.pop("stale")
        assert account_is_stale(bare) is True

    def test_an_explicitly_fresh_account_is_fresh(self):
        assert account_is_stale(account(stale=False)) is False


class TestWindowKeys:
    def test_canonical_provider_keys_land_in_the_right_columns(self):
        assert window_keys([account()]) == ("five_hour", "seven_day")

    def test_a_provider_that_names_its_windows_differently_keeps_its_names(self):
        # The contract says the keys are the PROVIDER's and are not fixed, so
        # an unknown key must not be relabelled into a column it may not mean.
        acct = account(windows={
            "rolling_hour": {"utilization": 0.1, "reset": False},
            "monthly": {"utilization": 0.2, "reset": False},
        })
        short, long = window_keys([acct])
        assert (short, long) == ("rolling_hour", "monthly")
        assert render.window_heading("rolling_hour") == "ROLLING"

    def test_no_windows_at_all_yields_no_keys(self):
        assert window_keys([account(windows={})]) == (None, None)


# ==========================================================================
# The table, and narrow terminals
# ==========================================================================


class TestTable:
    COLUMNS = [
        Column("a", "ALPHA", flex=True),
        Column("b", "BETA", drop=1),
        Column("c", "GAMMA", drop=2),
    ]
    # Long enough that the columns genuinely cannot all fit once the width
    # drops -- `Style.usable` has a floor of 32, so short cells never force
    # the interesting path.
    ROWS = [{"a": "alpha-value-one", "b": "beta-value-two", "c": "gamma-value-three"}]

    def test_nothing_is_dropped_when_it_all_fits(self):
        out = text(render_table(self.COLUMNS, self.ROWS, Style(width=80)))
        assert "ALPHA" in out and "BETA" in out and "GAMMA" in out

    def test_the_highest_drop_priority_goes_first(self):
        out = text(render_table(self.COLUMNS, self.ROWS, Style(width=40)))
        assert "GAMMA" not in out
        assert "BETA" in out
        assert "ALPHA" in out

    def test_a_zero_priority_column_is_never_dropped(self):
        out = text(render_table(self.COLUMNS, self.ROWS, Style(width=Style.MIN_WIDTH)))
        assert "ALPHA" in out

    @pytest.mark.parametrize("width", [80, 72, 60, 48, 40, 34])
    def test_no_line_ever_exceeds_the_budget(self, width):
        style = Style(width=width)
        for line in render_table(self.COLUMNS, self.ROWS, style):
            assert len(line) <= style.usable, f"{width}: {line!r}"

    def test_a_colour_cell_does_not_corrupt_the_column_arithmetic(self):
        # An SGR sequence is four invisible characters that len() counts and a
        # terminal does not; keeping tone beside the text is what stops that.
        plain = render_table(self.COLUMNS, self.ROWS, WIDE)
        toned = render_table(
            self.COLUMNS,
            [{"a": Cell(self.ROWS[0]["a"], "bad"),
              "b": Cell(self.ROWS[0]["b"], "warn"),
              "c": self.ROWS[0]["c"]}],
            WIDE,
        )
        assert "\x1b" not in text(toned)  # WIDE has colour off
        assert plain == toned


# ==========================================================================
# Accounts
# ==========================================================================


class TestAccounts:
    def test_the_columns_are_cs_statuss(self):
        out = text(render_accounts([account()], WIDE, NOW))
        for heading in ("ACCOUNT", "5H", "7D", "CLEARS", "STATE"):
            assert heading in out

    def test_a_stale_account_is_marked_and_the_legend_explains_the_mark(self):
        out = text(render_accounts([account(five=0.12, stale=True)], WIDE, NOW))
        assert "~12%" in out
        assert "projected from an older reading" in out

    def test_a_fresh_account_gets_no_legend(self):
        out = text(render_accounts([account()], WIDE, NOW))
        assert "projected" not in out

    def test_an_unreadable_pool_prints_no_figures_at_all(self):
        # The failure this guards: an empty table under a heading reads as
        # "nothing is wrong with the accounts".
        out = text(render_accounts(None, WIDE, NOW, error="403 forbidden"))
        assert "%" not in out
        assert "403 forbidden" in out
        assert "UNKNOWN, not zero" in out

    def test_an_empty_pool_says_so_rather_than_showing_nothing(self):
        out = text(render_accounts([], WIDE, NOW))
        assert "no accounts registered" in out
        assert "no subscription credential" in out

    def test_clears_follows_the_binding_window(self):
        # 97% on the 7-day, 2% on the 5-hour: the 7-day is what will stop this
        # account, so its reset is the one worth a column.
        acct = account(windows={
            "five_hour": {"utilization": 0.02, "resets_at": iso(hours=1), "reset": False},
            "seven_day": {"utilization": 0.97, "resets_at": iso(days=2), "reset": False},
        })
        out = text(render_accounts([acct], WIDE, NOW))
        assert "2d 00h" in out
        assert "1h 00m" not in out

    def test_clears_is_a_dash_when_nothing_was_measured(self):
        out = text(render_accounts([account(windows={})], WIDE, NOW))
        assert out.count(WIDE.dash) >= 3  # 5H, 7D and CLEARS all unmeasured
        assert "%" not in out

    def test_the_state_survives_a_reason_too_long_to_fit(self):
        # The STATE column truncates rather than disappearing: one account with
        # a wordy reason must not cost every other account its state. The full
        # reason is `sc trouble`'s job, and the test below holds it to that.
        acct = account(state="REAUTH_REQUIRED", reason="refresh token rejected")
        out = text(render_accounts([acct], WIDE, NOW))
        assert "reauth needed" in out

    def test_the_full_reason_is_recoverable_from_trouble(self):
        acct = account(state="REAUTH_REQUIRED", reason="refresh token rejected")
        findings = find_trouble(Snapshot(accounts=[acct], now=NOW), WIDE)
        assert any("refresh token rejected" in f.what for f in findings)

    def test_a_short_reason_is_shown_in_full(self):
        acct = account("a:1", state="PAUSED", reason="by hand")
        assert "paused" in text(render_accounts([acct], WIDE, NOW))
        assert "by hand" in text(render_accounts([acct], WIDE, NOW))

    def test_no_key_material_can_reach_the_screen(self):
        # The API shape carries none -- not even a length -- so anything a
        # payload smuggles in must not be rendered by accident.
        acct = account(access_token="sk-ant-secret", access_token_len=108)
        out = text(render_accounts([acct], WIDE, NOW))
        assert "sk-ant-secret" not in out
        assert "108" not in out

    def test_no_key_material_survives_the_json_path_either(self):
        # The screen is defended by construction -- it prints named columns --
        # so the surface that would show a token first is `sc --json`, which
        # dumps the payload as fetched. `public_account` is an ALLOW-list for
        # that reason: the field that leaks is the one nobody has thought of,
        # and `Credential.redacted()` already exposes a token's LENGTH, which
        # is a real hint about a secret.
        reduced = public_account(
            account(access_token="sk-ant-secret", access_token_len=108, refresh_token="rt")
        )
        assert "access_token" not in reduced
        assert "access_token_len" not in reduced
        assert "refresh_token" not in reduced
        assert reduced["account_id"] == "acme:main"
        assert reduced["stale"] is False

    def test_the_json_path_keeps_every_field_the_screen_reads(self):
        # An allow-list that drops something the renderer needs would make the
        # two surfaces disagree about the same account, which is the failure
        # it exists to prevent in the other direction.
        reduced = public_account(account(state="PAUSED", reason="by hand", lend_to=["other"]))
        for key in ("account_id", "label", "provider", "state", "reason",
                    "lend_to", "assigned", "observed_at", "stale", "windows"):
            assert key in reduced, key
        assert reduced["windows"]["five_hour"]["utilization"] == 0.12

    def test_the_json_path_strips_smuggled_fields_from_inside_a_window(self):
        acct = account(windows={"five_hour": {
            "utilization": 0.5, "resets_at": iso(hours=1), "reset": False,
            "access_token": "sk-ant-secret",
        }})
        assert public_account(acct)["windows"]["five_hour"] == {
            "utilization": 0.5, "resets_at": iso(hours=1), "reset": False,
        }

    @pytest.mark.parametrize("width", [80, 70, 60, 50, 42, 34])
    def test_it_fits_however_narrow_the_terminal_is(self, width):
        style = Style(width=width)
        accounts = [account("acme:main"), account("acme:spare", stale=True, five=0.99)]
        for line in render_accounts(accounts, style, NOW):
            assert len(line) <= style.usable, f"{width}: {line!r}"

    def test_the_stale_mark_survives_the_narrowest_terminal(self):
        # Columns may be dropped as the width falls; the mark that says a
        # number cannot be trusted may not be one of them.
        out = text(render_accounts([account(five=0.12, stale=True)], Style(width=36), NOW))
        assert "~12%" in out

    def test_subtitle_counts_assigned_agents_and_stale_readings(self):
        subtitle = render.accounts_subtitle(
            [account("a:1", **{"assigned": 2}), account("a:2", stale=True)], WIDE
        )
        assert "2 accounts" in subtitle and "2 agents" in subtitle and "1 stale" in subtitle

    def test_subtitle_says_unreadable_rather_than_zero(self):
        assert render.accounts_subtitle(None, WIDE) == "unreadable"


# ==========================================================================
# Capacity: which pool actually binds
# ==========================================================================


class TestBindingPool:
    REQUIRED = ["global", "tenant:acme", "resource:standard", "runner:claude-code",
                "backend:CLOUD_RUN_JOB", "provider:anthropic"]

    def test_the_tightest_pool_binds_not_the_global_one(self):
        pools = {p["name"]: p for p in [
            pool("global", limit=100, active=10),
            pool("tenant:acme", limit=20, active=2),
            pool("provider:anthropic", limit=8, active=6),
        ]}
        binding = binding_pool(self.REQUIRED, pools, units=1)
        assert binding.pool == "provider:anthropic"
        assert binding.room == 2

    def test_units_divide_the_headroom(self):
        # A `browser` task costs 2 units, so 5 free slots is 2 more tasks.
        pools = {"global": pool("global", limit=10, active=5)}
        assert binding_pool(["global"], pools, units=2).room == 2

    def test_a_paused_pool_binds_immediately(self):
        pools = {p["name"]: p for p in [
            pool("global", limit=100, active=0),
            pool("runner:claude-code", limit=50, active=0, enabled=False),
        ]}
        binding = binding_pool(self.REQUIRED, pools, units=1)
        assert binding.pool == "runner:claude-code"
        assert binding.paused is True
        assert binding.room == 0

    def test_an_absent_pool_is_unlimited_by_construction_not_unknown(self):
        # admission.py: a pool that was never configured is uncapped. That is
        # a fact about the platform, so it renders as infinity, not a mark.
        binding = binding_pool(self.REQUIRED, {}, units=1)
        assert binding.unconfigured is True
        assert render.format_room(binding, WIDE).text == WIDE.unlimited

    def test_a_pool_that_omits_enabled_is_not_assumed_open(self):
        # `.enabled // true` in jq reports a paused pool as open; the Python
        # spelling is `pool.get("enabled", True)`. Neither happens here.
        bare = pool("global", limit=5, active=1)
        bare.pop("enabled")
        assert render._tri_enabled(bare) is None
        assert binding_pool(["global"], {"global": bare}, units=1).unknown is True

    def test_a_pool_with_no_limit_reported_yields_a_mark_not_a_number(self):
        bare = pool("global")
        bare["effective_limit"] = None
        binding = binding_pool(["global"], {"global": bare}, units=1)
        assert binding.room is None
        assert render.format_room(binding, WIDE).text == WIDE.dash

    def test_one_ungradeable_pool_makes_the_whole_room_unknown(self):
        # Admission is all-or-nothing across every required pool, so a pool
        # that did not report its ceiling could be the one that refuses and
        # the true room could be 0. Falling through to the pool that DID
        # report prints a confident 8 whose only support is the silence of
        # the pool that might have contradicted it.
        nolimit = pool("global")
        nolimit["effective_limit"] = None
        pools = {"global": nolimit, "tenant:acme": pool("tenant:acme", limit=10, active=2)}
        binding = binding_pool(["global", "tenant:acme"], pools, units=1)
        assert binding.unknown is True
        assert binding.pool == "global"
        assert render.format_room(binding, WIDE).text == WIDE.dash

    def test_payload_order_cannot_change_the_answer(self):
        nolimit = pool("global")
        nolimit["effective_limit"] = None
        pools = {"global": nolimit, "tenant:acme": pool("tenant:acme", limit=10, active=2)}
        for order in (["global", "tenant:acme"], ["tenant:acme", "global"]):
            assert binding_pool(order, pools, units=1).unknown is True

    def test_a_pool_that_omits_enabled_gives_a_mark_not_a_confident_room(self):
        # It may be PAUSED, in which case the room is 0, not 7. The POOL table
        # already prints `? not reported` for this pool; the PROFILE row
        # underneath used to print a number anyway.
        bare = pool("global", limit=10, active=3)
        bare.pop("enabled")
        binding = binding_pool(["global", "tenant:acme"],
                               {"global": bare, "tenant:acme": pool("tenant:acme", limit=10, active=2)},
                               units=1)
        assert binding.unknown is True
        assert render.format_room(binding, WIDE).text == WIDE.dash
        # USED is still a measurement, and stays one.
        assert render.format_used(binding, WIDE).text == "3/10"

    def test_a_paused_pool_still_beats_an_unknown_one(self):
        # "It refuses, now" is more useful than "nothing is known", and it is
        # not a guess: the pool said so.
        nolimit = pool("global")
        nolimit["effective_limit"] = None
        pools = {"global": nolimit, "tenant:acme": pool("tenant:acme", enabled=False)}
        binding = binding_pool(["global", "tenant:acme"], pools, units=1)
        assert binding.paused is True and binding.pool == "tenant:acme"


class TestCapacityView:
    CAPACITY = {
        "tenant_id": "acme",
        "pools": [
            pool("global", limit=100, active=10),
            pool("tenant:acme", limit=20, active=2),
            pool("provider:anthropic", limit=8, active=6),
            pool("resource:standard", limit=50, active=5),
        ],
        "runner_profiles": {
            "claude-code": {
                "resource_class": "standard",
                "backend": "CLOUD_RUN_JOB",
                "provider": "anthropic",
                "units": 1,
                "pools": ["global", "tenant:acme", "resource:standard",
                          "runner:claude-code", "backend:CLOUD_RUN_JOB",
                          "provider:anthropic"],
            },
            "mock": {
                "resource_class": "standard",
                "backend": "CLOUD_RUN_JOB",
                "provider": None,
                "units": 1,
                "pools": ["global", "tenant:acme", "resource:standard",
                          "runner:mock", "backend:CLOUD_RUN_JOB"],
            },
        },
    }

    def test_it_names_the_pool_that_binds_each_profile(self):
        out = text(render_capacity(self.CAPACITY, WIDE))
        assert "BINDS ON" in out
        assert "provider:anthropic" in out
        # `mock` has no provider, so its tightest is the tenant pool.
        assert "tenant:acme" in out

    def test_the_subtitle_quotes_the_real_ceiling_not_the_global_one(self):
        assert render.capacity_subtitle(self.CAPACITY, WIDE) == "tightest: provider:anthropic 6/8"

    def test_the_subtitle_names_no_ceiling_when_a_required_pool_is_ungradeable(self):
        # The subtitle is the line people quote, so it must not quote a
        # tightest pool while another required pool's ceiling is unknown.
        nolimit = pool("global")
        nolimit["effective_limit"] = None
        capacity = dict(self.CAPACITY, pools=[nolimit, pool("tenant:acme", limit=10, active=2)])
        subtitle = render.capacity_subtitle(capacity, WIDE)
        assert subtitle == "global ceiling unknown"
        assert "tightest" not in subtitle

    def test_a_paused_pool_is_shown_as_paused_not_as_open(self):
        capacity = dict(self.CAPACITY)
        capacity["pools"] = [pool("global", limit=100, active=0, enabled=False)]
        out = text(render_capacity(capacity, WIDE))
        assert "paused" in out
        assert "open" not in out

    def test_a_pool_that_did_not_report_enabled_is_flagged(self):
        bare = pool("global", limit=5, active=1)
        bare.pop("enabled")
        capacity = dict(self.CAPACITY, pools=[bare])
        assert "not reported" in text(render_capacity(capacity, WIDE))

    def test_unreadable_capacity_prints_no_ceilings(self):
        out = text(render_capacity(None, WIDE, error="connection refused"))
        assert "connection refused" in out
        assert "BINDS ON" not in out

    @pytest.mark.parametrize("width", [80, 68, 56, 44, 36])
    def test_it_fits_however_narrow_the_terminal_is(self, width):
        style = Style(width=width)
        for line in render_capacity(self.CAPACITY, style):
            assert len(line) <= style.usable, f"{width}: {line!r}"


# ==========================================================================
# Agents
# ==========================================================================


def task(task_id="t_abcdef123456", state="RUNNING", **extra):
    payload = {
        "id": task_id,
        "state": state,
        "runner_profile": "claude-code",
        "created_at": iso(minutes=-30),
        "started_at": iso(minutes=-20),
        "metadata": {"unit": "lane-a"},
        "attempt_count": 1,
        "max_attempts": 3,
    }
    payload.update(extra)
    return payload


class TestAgents:
    def test_running_and_queued_are_both_shown(self):
        out = text(render_agents(
            [task(state="RUNNING"), task("t_q", state="QUEUED")], WIDE, NOW
        ))
        # Upper case, as the API spells a state (#88, SC-F19).
        assert "RUNNING" in out
        assert "QUEUED" in out

    def test_a_parked_task_says_why(self):
        out = text(render_agents(
            [task("t_p", state="PARKED", park_reason="PROVIDER_QUOTA_EXHAUSTED")],
            WIDE, NOW,
        ))
        assert "provider quota exhausted" in out

    def test_a_blocked_task_says_why_and_names_the_pool_when_there_is_room(self):
        blocked = [task("t_r", state="READY",
                        blocked_by=[{"pool": "provider:anthropic",
                                     "reason": "PROVIDER_CONCURRENCY_LIMIT"}])]
        # At 80 columns the reason is what survives; the pool name follows it
        # into an ellipsis rather than pushing the row past the budget.
        assert "provider concurrency limit" in text(render_agents(blocked, WIDE, NOW))
        wide = text(render_agents(blocked, Style(width=110), NOW))
        assert "provider:anthropic" in wide

    def test_an_id_arrives_under_either_key(self):
        # task_to_api says `id`; a dispatch response says `task_id`. Guessing
        # wrong does not error, it silently renders every field as absent.
        assert render.task_id_of({"task_id": "t_1"}) == "t_1"
        assert render.task_id_of({"id": "t_2"}) == "t_2"

    def test_an_unlistable_set_is_not_an_empty_one(self):
        out = text(render_agents(None, WIDE, NOW, error="504 gateway timeout"))
        assert "could not be listed" in out
        assert "504 gateway timeout" in out
        assert "nothing running" not in out

    def test_a_genuinely_idle_cluster_says_so(self):
        assert "nothing running, nothing queued" in text(render_agents([], WIDE, NOW))

    def test_subtitle_counts_by_bucket(self):
        subtitle = render.agents_subtitle(
            [task(state="RUNNING"), task("a", state="LEASED"), task("b", state="QUEUED")],
            WIDE,
        )
        # Both hold capacity, so both are ACTIVE; only one of them is running,
        # and the headline says so (#88, SC-F14).
        assert "2 active (1 leased, 1 running)" in subtitle and "1 queued" in subtitle

    @pytest.mark.parametrize("width", [80, 66, 52, 40, 34])
    def test_it_fits_however_narrow_the_terminal_is(self, width):
        style = Style(width=width)
        tasks = [task(), task("t_parked", state="PARKED", park_reason="CREDENTIAL_MISSING")]
        for line in render_agents(tasks, style, NOW):
            assert len(line) <= style.usable, f"{width}: {line!r}"


# ==========================================================================
# One task
# ==========================================================================


class TestTask:
    FINISHED = task(
        state="SUCCEEDED",
        result_summary={"git": {
            "base": "abc1234",
            "commit_count": 2,
            "insertions": 40,
            "deletions": 3,
            "commits": [{"sha": "deadbeefcafe", "subject": "the fix"}],
            "pull_request": {"number": 7, "url": "https://example.test/pr/7"},
        }},
    )

    def test_it_shows_commits_patch_and_pull_request(self):
        out = text(render_task(self.FINISHED, WIDE, NOW, patch="gs://bucket/p.patch"))
        assert "deadbeefca" in out
        assert "the fix" in out
        assert "gs://bucket/p.patch" in out
        assert "https://example.test/pr/7" in out

    def test_a_missing_pull_request_carries_its_reason(self):
        finished = task(state="SUCCEEDED", result_summary={"git": {
            "base": "abc1234", "commit_count": 1, "insertions": 1, "deletions": 0,
            "commits": [], "publish_reason": "no push credential for this tenant",
        }})
        out = text(render_task(finished, WIDE, NOW, patch="gs://b/p.patch"))
        assert "no push credential for this tenant" in out

    def test_a_missing_patch_carries_its_reason_rather_than_an_empty_field(self):
        out = text(render_task(
            task(state="FAILED", result_summary={"git": {"base": "abc"}}),
            WIDE, NOW, patch=None, no_patch_because="the attempt never reached a checkpoint",
        ))
        assert "the attempt never reached a checkpoint" in out

    def test_an_unreadable_task_is_not_an_empty_one(self):
        out = text(render_task(None, WIDE, NOW, error="no such task: t_x"))
        assert "could not be read" in out
        assert "no such task: t_x" in out

    def test_a_task_with_no_result_yet_says_nothing_recorded(self):
        out = text(render_task(task(state="RUNNING"), WIDE, NOW))
        assert "nothing recorded yet" in out
        assert "commits" not in out

    @pytest.mark.parametrize("width", [80, 60, 44, 34])
    def test_it_fits_however_narrow_the_terminal_is(self, width):
        style = Style(width=width)
        for line in render_task(self.FINISHED, style, NOW, patch="gs://bucket/patches/p.patch"):
            assert len(line) <= style.usable + len("gs://bucket/patches/p.patch"), line
        for line in render_task(self.FINISHED, style, NOW):
            assert isinstance(line, str)


# ==========================================================================
# Trouble
# ==========================================================================


def snapshot(**overrides) -> Snapshot:
    base = dict(
        tenant={"tenant_id": "acme", "email": "a@example.test"},
        stats={"dispatch_paused": False, "tasks_by_state": {}},
        capacity={"pools": [pool("global", limit=10, active=1)], "runner_profiles": {}},
        accounts=[account()],
        tasks=[task()],
        api_url="https://swarm-api.example.test",
        tier="iap",
        now=NOW,
    )
    base.update(overrides)
    return Snapshot(**base)


class TestTrouble:
    def test_a_healthy_cluster_has_nothing_to_report(self):
        assert find_trouble(snapshot(), WIDE) == []
        assert "nothing wrong right now" in text(render_trouble([], WIDE))

    def test_a_subsystem_that_could_not_be_read_is_itself_a_finding(self):
        # Silence about a subsystem is how an operator concludes it is fine.
        findings = find_trouble(
            snapshot(accounts=None, accounts_error="403 forbidden"), WIDE
        )
        assert any(f.where == "accounts" and "403 forbidden" in f.what for f in findings)
        assert findings[0].severity == "down"

    def test_an_absent_accounts_route_is_not_a_cluster_that_is_down(self):
        # Every deployment older than the accounts proxy answers 404 here. It
        # is unreadable, and saying so is right; grading it "down" turns the
        # first `sc trouble` on current production into a red screen about a
        # cluster that has not changed.
        findings = find_trouble(
            snapshot(
                accounts=None,
                accounts_error="GET /v1/accounts -> 404: Not Found",
                accounts_absent=True,
            ),
            WIDE,
        )
        accounts = [f for f in findings if f.where == "accounts"]
        assert accounts and accounts[0].severity == "note"
        assert "404" in accounts[0].what
        assert not any(f.severity == "down" for f in findings)

    def test_an_accounts_route_that_failed_is_still_down(self):
        # The downgrade is for "there is no such route", not for "it broke".
        findings = find_trouble(
            snapshot(accounts=None, accounts_error="GET /v1/accounts -> 500: boom"), WIDE
        )
        assert any(f.where == "accounts" and f.severity == "down" for f in findings)

    def test_paused_dispatch_is_the_loudest_thing_on_the_screen(self):
        findings = find_trouble(snapshot(stats={"dispatch_paused": True}), WIDE)
        assert any(f.where == "dispatch" and f.severity == "down" for f in findings)

    def test_a_stale_account_is_reported_with_its_age(self):
        findings = find_trouble(
            snapshot(accounts=[account(stale=True)]), WIDE
        )
        stale = [f for f in findings if "projections" in f.what]
        assert stale and "ago" in stale[0].what

    def test_an_account_never_read_says_so_rather_than_giving_an_age(self):
        findings = find_trouble(snapshot(accounts=[account(observed=False)]), WIDE)
        assert any("no reading has ever arrived" in f.what for f in findings)

    def test_a_dead_credential_is_a_down(self):
        findings = find_trouble(
            snapshot(accounts=[account(state="REAUTH_REQUIRED", reason="token revoked")]),
            WIDE,
        )
        assert any(f.severity == "down" and "token revoked" in f.what for f in findings)

    def test_an_exhausted_window_is_reported_with_when_it_clears(self):
        acct = account(windows={"seven_day": {
            "utilization": 1.0, "resets_at": iso(hours=5), "reset": False,
        }})
        findings = find_trouble(snapshot(accounts=[acct]), WIDE)
        exhausted = [f for f in findings if "7D at" in f.what]
        assert exhausted and "clears in 5h 00m" in exhausted[0].what

    def test_no_accounts_at_all_is_a_finding_not_a_blank(self):
        findings = find_trouble(snapshot(accounts=[]), WIDE)
        assert any("none registered" in f.what for f in findings)

    def test_a_closed_pool_is_a_down(self):
        capacity = {"pools": [pool("global", limit=0, active=0)], "runner_profiles": {}}
        findings = find_trouble(snapshot(capacity=capacity), WIDE)
        assert any(f.severity == "down" and "limit is 0" in f.what for f in findings)

    def test_a_full_pool_is_a_warning(self):
        capacity = {"pools": [pool("global", limit=4, active=4)], "runner_profiles": {}}
        findings = find_trouble(snapshot(capacity=capacity), WIDE)
        assert any("full at 4/4" in f.what for f in findings)

    def test_parked_tasks_are_grouped_by_reason(self):
        tasks = [
            task("t1", state="PARKED", park_reason="PROVIDER_QUOTA_EXHAUSTED"),
            task("t2", state="PARKED", park_reason="PROVIDER_QUOTA_EXHAUSTED"),
            task("t3", state="PARKED", park_reason="CREDENTIAL_MISSING"),
        ]
        findings = find_trouble(snapshot(tasks=tasks), WIDE)
        parked = [f.what for f in findings if f.where == "parked"]
        # Whose tasks they are is part of the finding (#88, SC-F5).
        assert "2 task(s) of tenant acme: provider quota exhausted" in parked
        assert "1 task(s) of tenant acme: credential missing" in parked

    def test_findings_are_ordered_worst_first(self):
        findings = find_trouble(
            snapshot(
                stats={"dispatch_paused": True},
                accounts=[account(stale=True)],
                tasks=[task("t1", state="FAILED")],
            ),
            WIDE,
        )
        assert [f.severity for f in findings] == sorted(
            [f.severity for f in findings], key=render.SEVERITIES.index
        )

    @pytest.mark.parametrize("width", [80, 62, 48, 38])
    def test_it_fits_however_narrow_the_terminal_is(self, width):
        style = Style(width=width)
        findings = find_trouble(
            snapshot(accounts=[account(state="REAUTH_REQUIRED", reason="the refresh token was rejected by the provider")]),
            style,
        )
        for line in render_trouble(findings, style):
            assert len(line) <= style.usable, f"{width}: {line!r}"

    LONG = (
        "unreadable - GET /v1/accounts -> 404: Not Found -- this deployment's "
        "swarm-api has no /v1/accounts route, so the account pool cannot be "
        "read from here. It is UNKNOWN, not empty"
    )

    @pytest.mark.parametrize("width", [80, 62, 48, 38])
    def test_a_long_reason_is_wrapped_rather_than_truncated(self, width):
        # A transport error is the longest string `sc` ever prints and the one
        # it most needs whole: truncated at the column edge it keeps the half
        # that says something is unreadable and loses the half that says what
        # to do. This view is the one an operator opens to read it.
        style = Style(width=width)
        lines = render_trouble([Finding("down", "accounts", self.LONG)], style)
        joined = " ".join(line.strip() for line in lines)
        assert self.LONG in joined
        assert style.ellipsis not in joined
        for line in lines:
            assert len(line) <= style.usable, f"{width}: {line!r}"

    def test_a_long_reason_is_no_shorter_here_than_in_the_overview(self):
        snap = snapshot(
            accounts=None, accounts_error=self.LONG.split(" - ", 1)[1], accounts_absent=True
        )
        overview = text(render_overview(snap, WIDE))
        trouble = text(render_trouble(find_trouble(snap, WIDE), WIDE))
        tail = "It is UNKNOWN, not empty"
        assert tail in overview
        assert tail in trouble

    def test_the_identity_of_a_finding_survives_a_reason_that_does_not_fit(self):
        long_where = "acme:an-account-with-a-very-long-identifier-indeed"
        lines = render_trouble([Finding("warn", long_where, self.LONG)], Style(width=80))
        assert lines[0].strip().startswith("!")
        assert "acme:an-account" in lines[0]


# ==========================================================================
# The whole screen
# ==========================================================================


class TestOverview:
    @pytest.mark.parametrize("width", [80, 72, 64, 56, 48, 40, 34])
    def test_every_line_fits_at_every_width(self, width):
        style = Style(width=width)
        snap = snapshot(
            accounts=[account("acme:main"), account("acme:spare", stale=True, five=0.99)],
            capacity=TestCapacityView.CAPACITY,
            tasks=[task(), task("t_p", state="PARKED", park_reason="CREDENTIAL_MISSING")],
        )
        for line in render_overview(snap, style):
            assert len(line) <= style.usable, f"{width}: {line!r}"

    def test_no_colour_codes_when_colour_is_off(self):
        out = text(render_overview(snapshot(), WIDE))
        assert "\x1b" not in out

    def test_colour_codes_appear_only_when_colour_is_on(self):
        out = text(render_overview(snapshot(accounts=[account(state="REAUTH_REQUIRED")]), COLOUR))
        assert "\x1b[" in out

    def test_the_ascii_screen_is_pure_ascii(self):
        # Every path, not just the bar glyphs: the marks that leak are the ones
        # nobody thinks to check -- separators in subtitles, and the em dash
        # inside a trouble finding's text.
        snap = snapshot(
            accounts=[
                account("acme:main", stale=True),
                account("acme:b", windows={}),
                account("acme:dead", state="REAUTH_REQUIRED", reason="token revoked"),
                account("acme:held", state="PAUSED", reason="by hand", lend_to=["other"]),
            ],
            capacity=None,
            capacity_error="connection refused",
            tasks=[task("t_p", state="PARKED", park_reason="CREDENTIAL_MISSING")],
        )
        text(render_overview(snap, ASCII)).encode("ascii")
        for finding in find_trouble(snap, ASCII):
            finding.what.encode("ascii")

    def test_the_ascii_task_screen_is_pure_ascii(self):
        text(render_task(TestTask.FINISHED, ASCII, NOW, patch="gs://b/p.patch")).encode("ascii")
        no_pr = task(state="SUCCEEDED", result_summary={"git": {
            "base": "abc", "commit_count": 1, "commits": [],
            "publish_reason": "no push credential",
        }})
        text(render_task(no_pr, ASCII, NOW, patch="gs://b/p.patch")).encode("ascii")

    def test_a_wholly_unreachable_cluster_renders_marks_and_no_numbers(self):
        # Every fetch failed. The screen must say so five times over rather
        # than showing five tidy zeroes.
        snap = Snapshot(
            tenant=None, tenant_error="401",
            stats=None, stats_error="401",
            capacity=None, capacity_error="401",
            accounts=None, accounts_error="401",
            tasks=None, tasks_error="401",
            now=NOW,
        )
        out = text(render_overview(snap, WIDE))
        assert "%" not in out
        assert out.count("could not be") >= 3
        assert len(find_trouble(snap, WIDE)) >= 5

    def test_the_header_drops_context_rather_than_overflowing(self):
        snap = snapshot(api_url="https://swarm-api-very-long-hostname-here.a.run.app")
        line = render.render_header(snap, Style(width=40))[0]
        assert len(line) <= 40


class TestTheFrozenContract:
    """The state sets are the contract's, not a copy of the contract's."""

    def test_running_states_are_the_contracts_concurrency_states(self):
        # "Only LEASED/DISPATCHED/STARTING/RUNNING create demand" is the rule
        # the whole platform is built on. A status tool with its own copy goes
        # on reporting the old answer for months after the contract gains a
        # state -- under-reporting the one invariant it exists to show.
        assert render.RUNNING_STATES == {s.value for s in CONCURRENCY_STATES}

    def test_queued_states_are_the_contracts_pending_states(self):
        assert render.QUEUED_STATES == {s.value for s in PENDING_STATES}

    def test_the_contract_is_not_shadowed_by_a_local_terminal_set(self):
        # `TERMINAL_STATES` here was a tuple with an extra member, shadowing a
        # frozenset of enum members under the same name, and read by nothing.
        assert not hasattr(render, "TERMINAL_STATES")

    def test_counting_running_agents_uses_those_states(self):
        tasks = [task(f"t{i}", state=state) for i, state in enumerate(sorted(render.RUNNING_STATES))]
        assert render.agents_subtitle(tasks, WIDE).startswith(f"{len(tasks)} active")


class TestTimeFormatting:
    def test_a_past_reset_reads_as_cleared(self):
        assert format_until(iso(hours=-1), NOW, WIDE) == "cleared"

    def test_an_absent_timestamp_is_a_mark(self):
        assert format_until(None, NOW, WIDE) == WIDE.dash

    def test_a_malformed_timestamp_is_a_mark_and_not_a_crash(self):
        assert format_until("not-a-time", NOW, WIDE) == WIDE.dash

    @pytest.mark.parametrize(
        "delta,expected",
        [
            ({"seconds": 30}, "<1m"),
            ({"minutes": 42}, "42m"),
            ({"hours": 4, "minutes": 35}, "4h 35m"),
            ({"days": 2, "hours": 22}, "2d 22h"),
        ],
    )
    def test_spans_read_the_way_cs_status_writes_them(self, delta, expected):
        assert format_until(iso(**delta), NOW, WIDE) == expected
