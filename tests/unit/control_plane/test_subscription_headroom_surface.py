"""SUBSCRIPTION HEADROOM read 100% while nothing had measured two of the three
accounts, and said so on the same card.

WHAT THE OWNER SAW, 2026-09-22 (`docs/web-ui/evidence/01-overview-now.png`):

    SUBSCRIPTION HEADROOM
    100 % left
    best of 1 usable account · its five-hour window binds
    every registered account has a current reading

WHAT WAS TRUE AT THAT MOMENT. Three accounts were registered. `eng:team` had a
reading -- the figure is its, and it is real. `u-bogdan:devops-main` and
`u-bogdan:personal` had `observed_at: null` and `windows: {}` and had never
been polled once, because `usagepoll.py:94` calls `access()` on a secret the
broker holds `secretVersionAdder` on and not `secretAccessor`, and has logged
`PermissionDenied` for them every five minutes for days. The poller fails SAFE
-- a failed read records nothing rather than a wrong number -- so the accounts
are not wrong, they are UNMEASURED.

THE DEFECT IS THEREFORE THE SENTENCE, NOT THE ARITHMETIC. `/v1/accounts` is
tenant-scoped; `accounts.length` is the accounts this tenant owns or was lent,
never the platform's. "every registered account has a current reading" turned a
tenant-wide list into a platform-wide claim, and it was selected by
`unusable === 0` rather than derived from what each account actually had. The
two accounts the sentence was silent about were the two worth naming.

AND THE FIGURE CARRIED NO AGE. Every other tile in that row says how old its
read is ("counted just now", "summed just now"). The one tile whose number is a
provider measurement rather than a platform count said nothing, so a reading
taken four days ago and one taken forty seconds ago rendered identically.

THE HOUSE RULE THIS BROKE. Two screens away the product already gets this
right: the task drawer writes "not reported -- No attempt reported a cost. This
is an absent measurement, not $0.00.", and `QuotaDetail.tsx` writes that UNKNOWN
"means no worker has reported on this provider for this tenant recently. That
is an absence of information, not an assurance." The headroom tile is held to
its own codebase's rule here.

WHY THIS READS THE SHIPPED TYPESCRIPT AS TEXT. The same reason
`test_blocker_ui_surface.py`, `test_runtimes_screen.py` and
`test_dispatch_ui_surface.py` do: no node, no network, no credentials, and a
mock of the screen would agree with whatever the screen happens to do. The
limitation is real and is stated rather than hidden -- these are assertions
about the decision the source makes, not about pixels. A render assertion needs
a DOM and a JS test runner, and `make test` installs neither and must stay
offline; `docs/web-ui/ui-audit-and-build-prompt.md` §B11.1 records that choice
as the owner's to make, so it is not made here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
UI = ROOT / "apps/swarm-ui/src"

#: Skipped rather than failed when the UI is not checked out, matching the
#: existing seam tests: this suite has to pass in a tree that holds only the
#: Python components.
pytestmark = pytest.mark.skipif(not UI.is_dir(), reason="apps/swarm-ui is not checked out")


def src(name: str) -> str:
    path = UI / name
    # A missing or empty client file must not skip or pass: a rename this test
    # cannot follow is exactly the silent hole it exists to close.
    assert path.is_file(), f"{path} is not present; this test would check nothing"
    text = path.read_text()
    assert text.strip(), f"{path} is empty; this test would check nothing"
    return text


def body_of(text: str, signature: str) -> str:
    """One top-level function's source, signature to closing brace.

    Terminated on a `}` in column 1 rather than by counting braces, the same
    way `test_blocker_ui_surface.py` does it: every function read here is
    top-level, so its closing brace is the only one at the start of a line.
    """
    start = text.index(signature)
    end = text.index("\n}\n", start)
    return text[start : end + 3]


def uncommented(body: str) -> str:
    """The body with `//` comments stripped.

    The comments here quote the wording they replaced -- "every registered
    account has a current reading" appears in a comment explaining why it is
    gone. A substring test over the raw body would read that quotation as the
    defect still being shipped, which is the failure mode
    `test_an_incomplete_list_says_so` warns about in the other direction.
    """
    return re.sub(r"^\s*//.*$", "", body, flags=re.MULTILINE)


def loop_of(body: str) -> str:
    """The per-account classification loop inside `accountHeadroom`."""
    start = body.index("for (const a of accounts) {")
    return body[start : body.index("\n  }\n", start)]


# ---------------------------------------------------------------------------
# 1. An unmeasured account is not a full one
# ---------------------------------------------------------------------------

def test_a_headroom_with_no_reading_renders_as_absent_and_never_as_100():
    """THE ONE THIS LANE EXISTS FOR.

    Every account is classified before any figure is taken, and the only
    classification that can produce a figure is `live`. When none does, the
    tile returns `pct: null` and an `absent` word -- never a percentage, and
    least of all 100, which is the number an unmeasured account would produce
    if `utilization` defaulted to zero anywhere along the path.
    """
    fn = uncommented(body_of(src("Overview.tsx"), "function accountHeadroom("))
    loop = loop_of(fn)

    # The two ways a reading can be missing, both routed to `unread`.
    assert "a.observed_at === null" in loop, (
        "an account that has never been polled must be recognised; "
        "`observed_at: null` is what the live platform serves for the two "
        "u-bogdan accounts usagepoll.py cannot read"
    )
    assert "w === null" in loop, "an account whose binding window is absent must be recognised too"
    assert loop.count("unread.push(a.label)") == 3, (
        "never-observed, no-binding-window and readingOf's never/absent must "
        "all land in `unread`; a path that skips it silently becomes headroom"
    )

    # EXACTLY ONE assignment to `best`, and it is downstream of the `live`
    # gate. This is the assertion the mutation has to get past: giving an
    # unread account a figure means assigning `best` somewhere else, and any
    # second assignment fails here.
    assert loop.count("best = {") == 1, (
        "`best` is assigned in exactly one place, after the live gate; a "
        "second assignment is an unmeasured account being given a figure"
    )
    gate = loop.index("if (r.kind !== 'live')")
    assert gate < loop.index("best = {"), "the live gate must precede the only figure"

    # And the no-figure return is null, with a word, and no percentage.
    absent_branch = fn[fn.index("if (best === null) {") : fn.index("\n  return {\n    pct: best.pct")]
    assert "pct: null" in absent_branch, (
        "no reading means no figure. `pct: 100` here is the defect this test "
        "is named after"
    )
    assert "absent: 'nothing measured'" in absent_branch
    assert not re.search(r"pct:\s*\d", absent_branch), (
        "the absent branch must not carry a numeric headroom at all"
    )


def test_the_unread_accounts_are_named_and_not_merely_counted():
    """"1 account excluded" does not tell anyone which credential to look at."""
    fn = uncommented(body_of(src("Overview.tsx"), "function absenceSentence("))
    assert "unread.join(', ')" in fn, "the accounts with no reading must be named"
    assert "headroom is unknown rather than full" in fn, (
        "the absent-value voice this product already uses elsewhere: an "
        "absence of information, not an assurance"
    )
    # The three situations stay three sentences. Collapsing them was the old
    # wording's mistake -- and it did not even list the case that applied.
    assert "stale or cleared" in fn
    assert "not serving" in fn


def test_the_tile_no_longer_asserts_that_every_account_has_a_reading():
    """The claim is derived from the three lists, and it is scope-bounded.

    `/v1/accounts` is tenant-scoped. "every REGISTERED account" was a
    platform-wide claim made from a tenant-wide list, and on 2026-09-22 it was
    printed for tenant `eng` while two accounts in `u-bogdan` had never been
    polled.
    """
    fn = uncommented(body_of(src("Overview.tsx"), "function accountHeadroom("))
    assert "every registered account has a current reading" not in fn, (
        "the asserted sentence must be gone, not reworded around"
    )
    assert "state.data.tenant_id" in fn, "the tile must know the scope it is counting"
    # The positive claim is reachable only when all three absence lists are
    # empty -- that is what makes it derived rather than chosen.
    assert "unread.length > 0 || projected.length > 0 || notServing.length > 0" in fn
    # The verb agrees with the count, so the phrase is interpolated rather than
    # literal -- "all 1 account in eng have a current reading" was the first
    # draft of this fix and it is the same carelessness in miniature.
    assert "a current reading" in fn
    assert "accounts.length === 1 ? 'has' : 'have'" in fn


# ---------------------------------------------------------------------------
# 2. A reading has an age, and the age is part of the figure
# ---------------------------------------------------------------------------

def test_a_stale_reading_is_labelled_with_its_age():
    """Stale, cleared AND current. The third was the one missing.

    `stale ... ago` has been on the pool panel for as long as it has existed.
    The `live` row printed only the window name, so the one row carrying a
    confident figure was the one row that did not say when it was measured --
    and "live" spans anything up to the broker's 30-minute staleness window.
    """
    body = uncommented(body_of(src("Overview.tsx"), "function AccountsBody("))
    assert "`stale ${timeAgo(r.observedAt)}`" in body, (
        "a stale reading must carry its age; the bare word 'stale' does not "
        "distinguish 31 minutes from four days"
    )
    assert "`${windowName} · ${timeAgo(r.observedAt)}`" in body, (
        "a CURRENT reading must carry its age too -- it is the row with a "
        "figure on it"
    )
    assert "timeAgo(r.resetsAt)" in body, "a cleared window must say when it cleared"


def test_the_headroom_figure_carries_the_age_of_the_reading_behind_it():
    """The tile's percentage is one account's provider reading, not a count.

    Every other tile in that row states the age of its read. This one printed a
    percentage with nothing saying whether it was measured a minute or four
    days ago, which is the same claim-without-provenance the absent-value copy
    on the task drawer is careful to avoid.
    """
    fn = uncommented(body_of(src("Overview.tsx"), "function accountHeadroom("))
    assert "observedAt: r.observedAt" in fn, (
        "the winning reading's timestamp has to survive the reduction to one "
        "number, or the tile has nothing to date the figure with"
    )
    assert "read ${timeAgo(best.observedAt)}" in fn, (
        "the figure's own age belongs beside the figure"
    )


def test_an_age_over_two_days_is_reported_in_days():
    """`96h ago` is arithmetic; `4d ago` is an answer.

    `timeAgo` ended at hours, so a reading four days old -- which is exactly
    what a never-refreshed account produces once the poller starts failing --
    printed as a two-digit hour count on the figures where age is the whole
    point. The 48-hour threshold is `humaniseUntil`'s in types.ts, so a
    duration and an age agree about where hours stop being readable.
    """
    fn = uncommented(body_of(src("Shell.tsx"), "export function timeAgo("))
    assert "h < 48" in fn, "hours stop at two days, matching humaniseUntil"
    assert "d ago" in fn, "older than that is reported in days"
    assert "h ago" in fn and "m ago" in fn and "s ago" in fn, (
        "the finer units must survive: this is an addition, not a replacement"
    )


# ---------------------------------------------------------------------------
# 3. The rest of the screen tells the same story
# ---------------------------------------------------------------------------

def test_the_accounts_check_still_names_the_never_polled_accounts():
    """The Needs-attention panel already got this right. It has to stay right.

    This check is what put "1 accounts have never been polled -- devops-main,
    personal" on the screen when the headroom tile above it said everything
    had a reading. The contradiction was the tile's fault; this pane is the
    reference behaviour and a regression here would remove the only place the
    two accounts appeared at all.
    """
    fn = uncommented(body_of(src("Overview.tsx"), "function accountCheck("))
    assert "a.observed_at === null" in fn
    assert "never.map((a) => a.label).join(', ')" in fn, "it must name them"
    assert "unknown rather than zero" in fn
    assert "cannot be counted as headroom" in fn


def test_no_sentence_on_this_screen_counts_one_account_as_plural():
    """"1 accounts, all with a current reading" was on the shipped screenshot.

    Cosmetic on its own; not cosmetic beside a figure that is being read as a
    measurement, because a sentence that cannot count to one is a sentence a
    reader stops trusting.
    """
    text = src("Overview.tsx")
    for signature in (
        "function accountHeadroom(",
        "function accountCheck(",
        "function AccountsBody(",
    ):
        fn = uncommented(body_of(text, signature))
        # `}` closing an interpolation, then a hard-coded plural noun. That is
        # the shape that printed "1 accounts" on the shipped screenshot, and
        # it is invisible to a test that only looks for one punctuation mark
        # after it -- the first version of this assertion checked for a
        # backtick and a newline and sailed straight past `} accounts,`.
        offender = re.search(r"\}\s*accounts\b", fn)
        assert offender is None, (
            f"{signature} interpolates a count straight onto a plural noun "
            f"({fn[max(0, offender.start() - 40) : offender.end() + 20]!r}); "
            "use countOf()"
        )
    helper = uncommented(body_of(text, "function countOf("))
    assert "n === 1 ? '' : 's'" in helper
