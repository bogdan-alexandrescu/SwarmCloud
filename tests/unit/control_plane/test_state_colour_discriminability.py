"""The state colours carried the entire signal and were one tone in greyscale.

THE DEFECT. `apps/swarm-ui/src/styles.css` spread `--ok`, `--warn` and `--bad`
by HUE and by nothing else. Measured as relative luminance (WCAG 2.x), the
shipped tokens were:

    light   ok  .1567   warn .1657   bad .1461
            ok vs bad 1.054:1   ok vs warn 1.043:1   bad vs warn 1.100:1
    dark    ok  .3633   warn .3660   bad .2632
            ok vs warn 1.006:1  -- essentially identical

A contrast ratio of 1.0 means the two colours are the SAME GREY. So in the
light theme all three states printed as one tone, and in the dark theme healthy
and warning printed as one tone. To a colour-blind operator, in a greyscale
print, or in the screenshot that gets pasted into an incident ticket, a healthy
platform and a failing one looked the same -- and on these screens the colour
was frequently the only signal: `.ctl-metric.is-good` / `.is-alert` differed
only in the colour of the figure, `.ctl-chip.is-ok` / `.is-warn` / `.is-bad` /
`.is-info` only in the colour of the word, `.ctl-util-fill.is-warn` / `.is-bad`
only in the colour of a 10px bar, and `.ctl-table tbody tr.is-bad` / `.is-warn`
only in an 8% wash, which is about a 1% change in tone.

What was NOT wrong, so the fix does not chase it: both colours already passed
WCAG AA against white for TEXT legibility. The defect was ok-vs-warn-vs-bad
discriminability, not text contrast -- and `test_state_text_contrast_still_passes_aa`
below exists so that a future widening of the spread cannot buy it by breaking
the thing that was already right.

THE TWO FLOORS THIS FILE PINS.

`MIN_STATE_RATIO = 1.5`, pairwise between ok, warn and bad, in BOTH themes.
Why 1.5 and not something else:

  * The measured defect ran from 1.006 to 1.100. Anything at or under about
    1.2 is not a visible step, so a floor in that region would ratify the bug.
  * WCAG 1.4.11 asks 3:1, but it asks it of a graphical object against its
    BACKGROUND, not of two foreground marks against each other, and 3:1 between
    all three is arithmetically impossible here. In the light theme a state
    colour is drawn as 10.5-12px text on `--surface-2` (L .8928), so the AA
    text floor caps the lightest of the three at L .1595; three colours each
    3:1 from the next need a 9:1 span, which puts the darkest at L -.0267.
    A test asserting 3:1 would assert something no palette can satisfy.
  * 1.5 is the largest round step that still leaves each of the three
    recognisable as its own hue. At 1.5 the light theme's darkest state sits at
    L .0341 (#681117, a dark red). At 1.75 it would sit at L .0184 (#4c0c11),
    which is a near-black that no longer reads as red at all.

`MIN_PAUSED_RATIO = 1.2`, between `--paused` and each of the triad. Weaker on
purpose: five colours cannot all be 1.5 apart inside the band the AA text floor
leaves (in the light theme that band is L 0 to .1595, a total span of 4.19:1,
so four colours at a uniform step manage 1.61 only by putting the darkest at
L .0121, i.e. black). PARKED is nevertheless a state an operator acts on
differently from FAILED, and the two shipped at 1.000:1 in the dark theme --
the same grey, exactly -- so it gets the floor the band can actually carry, and
the shape cues carry the rest.

`--info` is deliberately NOT in the triad. It is the link and neutral accent
(`.sub button`, `.ov-link`), not a severity -- and no longer the default
`.ctl-util-fill`, which is `--text-dim` (design-system.md §6.4) -- and
re-toning it would move every link on every screen. It is held to the AA text
floor and to a distinct chip silhouette, and nothing more.

AND SHAPE, NOT ONLY TONE. Tone alone cannot carry this: the light theme's band
is capped from above by the AA text floor, so widening the spread costs hue
identity (`--warn` now reads brown, `--bad` maroon), and there is no arithmetic
that gives five states five clearly separate greys. So every state also carries
a SILHOUETTE, and `test_every_chip_state_has_its_own_silhouette` and the three
tests after it assert that no two states are drawn as the same shape. The
product had already solved this once and used it in exactly one place:

    /* UNKNOWN IS NOT HEALTHY and must not borrow a healthy colour. Hollow
       dot, faint text: an absence of information, drawn as one. */
    .ctl-chip.is-unknown > i { background: transparent; ... }

These tests extend that idiom rather than inventing a second one.

Everything here reads the shipped stylesheet. A fixture repeating the hex
values would agree with whatever the fixture happens to say, which is the false
positive this repository has already produced once.
"""

from __future__ import annotations

import functools
import itertools
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CSS_PATH = ROOT / "apps/swarm-ui/src/styles.css"

pytestmark = pytest.mark.skipif(
    not CSS_PATH.is_file(), reason="apps/swarm-ui/src/styles.css is not present"
)

#: Pairwise floor between ok / warn / bad, in both themes. See the module
#: docstring for why this number and not 3:1.
MIN_STATE_RATIO = 1.5

#: Floor between --paused and each of the triad. Lower than MIN_STATE_RATIO
#: because the band the AA text floor leaves cannot hold five colours 1.5
#: apart; the shape cues carry the rest.
MIN_PAUSED_RATIO = 1.2

#: WCAG AA for normal text. These tokens are used at 10.5-12px, so the
#: large-text exemption does not apply to any of them.
AA_TEXT = 4.5

#: The severity axis. Order matters only for reading the failure message.
TRIAD = ("ok", "warn", "bad")

#: Every token that names a state, including the two that are not severities.
STATE_TOKENS = ("ok", "warn", "bad", "info", "paused")

#: The surfaces a state colour is drawn as text on.
SURFACES = ("bg", "surface", "surface-2")


# ---------------------------------------------------------------------------
# Colour arithmetic. WCAG 2.x relative luminance and contrast ratio.
# ---------------------------------------------------------------------------

def relative_luminance(hex_colour: str) -> float:
    """WCAG 2.x relative luminance of a `#rrggbb` string.

    This is the greyscale value: it is what survives a black-and-white print,
    a screenshot run through a colour-blindness simulator, and the eye of a
    reader with no red/green discrimination. Two colours with the same
    luminance are the same mark.
    """
    digits = hex_colour.lstrip("#")
    assert len(digits) == 6, f"expected #rrggbb, got {hex_colour!r}"
    channels = [int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [
        c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio between two `#rrggbb` strings, 1.0 to 21.0."""
    la, lb = relative_luminance(a), relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


# ---------------------------------------------------------------------------
# A very small CSS reader. Enough for this file and nothing more.
# ---------------------------------------------------------------------------

Rule = tuple[str, str, dict[str, str]]  # (at-rule prelude, selector, declarations)


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _declarations(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in body.split(";"):
        if ":" not in chunk:
            continue
        prop, _, value = chunk.partition(":")
        out[prop.strip()] = " ".join(value.split())
    return out


def _split_top(text: str, sep: str = ",") -> list[str]:
    """`text` split at every `sep` that is not inside parentheses, brackets or quotes.

    A selector list is not `str.split(",")`. `:where(.app input, .app select)`
    is ONE selector, and splitting it naively produced two fragments that
    matched nothing and parsed as nothing -- harmless while every assertion
    here looked selectors up by exact text, and wrong the moment one asks
    what a rule can reach.
    """
    parts: list[str] = []
    buf: list[str] = []
    depth, quote = 0, ""
    for char in text:
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(char)
    parts.append("".join(buf))
    return parts


def parse_rules(css: str) -> list[Rule]:
    """Every rule in the sheet, as (enclosing at-rules, one selector, decls).

    A comma-separated selector list is expanded into one entry per selector,
    because that is how the assertions below want to ask the question. The
    split is at TOP-LEVEL commas only (`_split_top`).
    """
    css = _strip_comments(css)
    rules: list[Rule] = []
    stack: list[str] = []
    buf: list[str] = []
    for char in css:
        if char == "{":
            stack.append("".join(buf).strip())
            buf = []
        elif char == "}":
            prelude = stack.pop()
            body = "".join(buf)
            buf = []
            if not prelude.startswith("@"):
                at_rules = " ".join(p for p in stack if p.startswith("@"))
                decls = _declarations(body)
                for selector in _split_top(prelude):
                    selector = " ".join(selector.split())
                    if selector:
                        rules.append((at_rules, selector, decls))
        else:
            buf.append(char)
    assert not stack, "unbalanced braces in styles.css"
    return rules


@pytest.fixture(scope="module")
def rules() -> list[Rule]:
    return parse_rules(CSS_PATH.read_text(encoding="utf-8"))


def _theme_palette(rules: list[Rule], light: bool) -> dict[str, str]:
    """The `--name: #hex` tokens in effect for one theme.

    Dark is the base `:root`. Light is that, overlaid with the `:root` inside
    `@media (prefers-color-scheme: light)` -- which is how the browser resolves
    it, so a token the light block forgets to override is reported with the
    dark value rather than as missing.
    """
    palette: dict[str, str] = {}
    for at_rules, selector, decls in rules:
        if not selector.startswith(":root"):
            continue
        in_light_media = "prefers-color-scheme: light" in at_rules
        if in_light_media and not light:
            continue
        if in_light_media and light:
            pass
        elif at_rules:
            # Some other at-rule (reduced motion, a width breakpoint). Not a
            # theme definition.
            continue
        for prop, value in decls.items():
            if prop.startswith("--") and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
                palette[prop[2:]] = value.lower()
    return palette


@pytest.fixture(scope="module")
def palettes(rules: list[Rule]) -> dict[str, dict[str, str]]:
    out = {"dark": _theme_palette(rules, light=False),
           "light": _theme_palette(rules, light=True)}
    for theme, palette in out.items():
        missing = [t for t in (*STATE_TOKENS, *SURFACES) if t not in palette]
        assert not missing, (
            f"the {theme} theme in {CSS_PATH.name} defines no "
            f"{', '.join('--' + m for m in missing)}"
        )
    return out


# ---------------------------------------------------------------------------
# 1. Tone. The triad must be separable in greyscale.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("theme", ["dark", "light"])
def test_severity_triad_is_separable_in_greyscale(palettes, theme):
    """ok, warn and bad must be MIN_STATE_RATIO apart from each other.

    This is the regression guard the defect asks for: someone nudging one of
    these three hexes toward a neighbour makes this fail by name rather than
    quietly restoring a screen on which a healthy platform and a failing one
    look identical.
    """
    palette = palettes[theme]
    failures = []
    for a, b in itertools.combinations(TRIAD, 2):
        ratio = contrast_ratio(palette[a], palette[b])
        if ratio < MIN_STATE_RATIO:
            failures.append(
                f"--{a} {palette[a]} (L {relative_luminance(palette[a]):.4f}) vs "
                f"--{b} {palette[b]} (L {relative_luminance(palette[b]):.4f}) "
                f"= {ratio:.3f}:1"
            )
    assert not failures, (
        f"the {theme} theme's state colours are not separable in greyscale "
        f"(floor {MIN_STATE_RATIO}:1); 1.000:1 means IDENTICAL in a black-and-"
        f"white print or to a colour-blind reader:\n  " + "\n  ".join(failures)
    )


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_parked_is_not_the_same_tone_as_the_triad(palettes, theme):
    """PARKED is an operator decision; FAILED is not. They shipped identical.

    `--paused` measured 1.000:1 against `--bad` in the dark theme and 1.001:1
    against `--ok` in the light theme -- the same grey to four decimal places
    in both cases.
    """
    palette = palettes[theme]
    failures = []
    for other in TRIAD:
        ratio = contrast_ratio(palette["paused"], palette[other])
        if ratio < MIN_PAUSED_RATIO:
            failures.append(f"--paused vs --{other} = {ratio:.3f}:1")
    assert not failures, (
        f"the {theme} theme draws --paused at the same tone as a severity "
        f"(floor {MIN_PAUSED_RATIO}:1):\n  " + "\n  ".join(failures)
    )


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_state_text_contrast_still_passes_aa(palettes, theme):
    """Widening the spread must not be bought by breaking text legibility.

    Every state token is drawn as text at 10.5-12px -- a chip word, a metric
    figure, a status column -- so AA is 4.5:1 and the large-text exemption
    never applies. This is the other half of the pin: without it, "make the
    tones further apart" has an easy wrong answer, which is to darken one until
    nobody can read it.

    It also catches a defect that was already shipped: the light theme's
    `--warn` was #9a6700, which measured 4.37:1 against `--surface-2`.
    """
    palette = palettes[theme]
    failures = []
    for token in STATE_TOKENS:
        for surface in SURFACES:
            ratio = contrast_ratio(palette[token], palette[surface])
            if ratio < AA_TEXT:
                failures.append(
                    f"--{token} {palette[token]} on --{surface} "
                    f"{palette[surface]} = {ratio:.2f}:1"
                )
    assert not failures, (
        f"the {theme} theme draws a state colour as text below the "
        f"{AA_TEXT}:1 AA floor:\n  " + "\n  ".join(failures)
    )


# ---------------------------------------------------------------------------
# 2. Shape. No two states may be drawn as the same silhouette.
# ---------------------------------------------------------------------------
#
# The properties that make a shape. `color` is deliberately absent: that is the
# signal these tests exist because the product relied on alone.
SHAPE_PROPS = (
    "width", "height", "border-radius", "clip-path", "transform",
    "background", "background-image", "background-size", "border",
    "box-shadow", "animation", "content", "border-style", "border-width",
)

CHIP_STATES = ("is-ok", "is-warn", "is-bad", "is-info", "is-paused",
               "is-unknown", "is-live")


def _strip_colours(value: str) -> str:
    """A declaration with every colour removed, leaving only its geometry.

    THIS IS THE POINT OF THE WHOLE FILE, applied to the test itself. Comparing
    raw declarations makes two marks that differ ONLY in colour look different
    to the test -- `linear-gradient(var(--bad), var(--bad))` against
    `linear-gradient(var(--warn), var(--warn))` -- which is precisely the bug
    being tested for, passing itself off as the fix. That false positive was
    live here until a mutation caught it: giving the warn row the same
    full-height edge rule as the bad row left the shape test green.

    So the colours come out first and what is left is the angle, the stop
    positions, the radii and the sizes -- the part a greyscale reader keeps.
    """
    out: list[str] = []
    i = 0
    while i < len(value):
        if value.startswith("color-mix(", i):
            depth, j = 0, i + len("color-mix")
            while j < len(value):
                if value[j] == "(":
                    depth += 1
                elif value[j] == ")":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
            i = j
            continue
        out.append(value[i])
        i += 1
    stripped = "".join(out)
    stripped = re.sub(r"var\(--[A-Za-z0-9-]+\)", "", stripped)
    stripped = re.sub(r"rgba?\([^()]*\)", "", stripped)
    stripped = re.sub(r"#[0-9a-fA-F]{3,8}\b", "", stripped)
    stripped = re.sub(r"\b(currentColor|transparent)\b", "", stripped,
                      flags=re.IGNORECASE)
    return " ".join(stripped.split())


def _shape_of(rules: list[Rule], selectors: tuple[str, ...]) -> dict[str, str]:
    """The shape declarations a mark ends up with, cascading in file order.

    `selectors` is given base-first: the generic rule, then the state's own
    override. Only unmediated rules count -- a `@media` block is a breakpoint
    or a motion preference, not the shape the mark has by default.

    Values come back with their colours stripped, so every comparison below is
    a comparison of FORM.
    """
    shape: dict[str, str] = {}
    for at_rules, selector, decls in rules:
        if at_rules or selector not in selectors:
            continue
        for prop, value in decls.items():
            if prop in SHAPE_PROPS:
                shape[prop] = _strip_colours(value)
    return shape


def test_every_chip_state_has_its_own_silhouette(rules):
    """Seven states, seven shapes.

    `.ctl-chip > i` used to be one 6px disc for every tone, so the chips were
    separated by colour and by nothing else. Each state now overrides the
    disc -- triangle, diamond, bar, two bars, hollow disc, haloed disc -- and
    two states sharing a silhouette is a failure here.

    `is-ok` keeps the plain disc, which is the base rule and correct: "present,
    and fine" is the state that should look like nothing special. It is the
    others that must move away from it.
    """
    shapes = {
        state: _shape_of(rules, (".ctl-chip > i", f".ctl-chip.{state} > i"))
        for state in CHIP_STATES
    }
    base = _shape_of(rules, (".ctl-chip > i",))
    assert base, "`.ctl-chip > i` defines no shape at all"

    collisions = []
    for a, b in itertools.combinations(CHIP_STATES, 2):
        if shapes[a] == shapes[b]:
            collisions.append(f"{a} and {b} are both drawn as {shapes[a]}")
    assert not collisions, (
        "two chip states are drawn as the same shape, so colour is again the "
        "only thing telling them apart:\n  " + "\n  ".join(collisions)
    )

    # And the six that are not `is-ok` must actually override the base, rather
    # than passing the test above by accident of some unrelated property.
    not_overridden = [
        state for state in CHIP_STATES
        if state != "is-ok" and shapes[state] == base
    ]
    assert not not_overridden, (
        "these chip states inherit the plain disc and so carry no shape cue: "
        + ", ".join(not_overridden)
    )


def test_metric_tiles_mark_good_and_alert_with_a_shape(rules):
    """`.ctl-metric.is-good` and `.is-alert` differed only in a text colour.

    Both now carry a corner mark, in the chip vocabulary: a disc for ok, a
    diamond for bad. The mark has to be REAL -- a pseudo-element with no
    `content` renders nothing -- and the two marks have to differ by something
    that is not their fill.
    """
    # THE MARK MOVED, AND THE RULE DID NOT. The landing-page redesign took the
    # box away from the metric -- there is no card, so there is no corner for a
    # corner mark to sit in -- and the mark went to the label's baseline:
    #
    #     .ctl-metric.is-good  .ctl-metric-label::after
    #     .ctl-metric.is-alert .ctl-metric-label::after
    #
    # The property this test exists for is untouched: both states carry a REAL
    # pseudo-element with a size, and the two differ by GEOMETRY and not only
    # by fill, so the page still reads in greyscale. Only the selector moved,
    # so only the selector is updated. Pinning the old one would have this test
    # failing on a redesign that kept every guarantee it asserts.
    good = _shape_of(rules, (".ctl-metric.is-good .ctl-metric-label::after",))
    alert = _shape_of(rules, (".ctl-metric.is-alert .ctl-metric-label::after",))

    for name, shape in (("is-good", good), ("is-alert", alert)):
        assert "content" in shape, (
            f"`.ctl-metric.{name} .ctl-metric-label::after` sets no `content`, so "
            f"the mark never renders and the figure is back to being colour-only"
        )
        assert {"width", "height"} <= set(shape), (
            f"`.ctl-metric.{name} .ctl-metric-label::after` has no size"
        )

    geometry = {"border-radius", "clip-path", "transform"}
    good_geom = {k: v for k, v in good.items() if k in geometry}
    alert_geom = {k: v for k, v in alert.items() if k in geometry}
    assert good_geom != alert_geom, (
        "the good and alert figures carry the same shape of mark, so colour is "
        f"again the only signal: both are {good_geom}"
    )


def test_utilisation_fills_differ_by_texture_not_only_hue(rules):
    """A 10px bar has no room for a glyph, so the cue is in the fill.

    `.ctl-util-fill.is-warn` and `.is-bad` were a flat `background` and nothing
    else, which is one flat bar in greyscale. Each tone now carries its own
    stripe pattern, and the default (under the ceiling) carries none.
    """
    patterns = {}
    for tone in ("is-warn", "is-bad", "is-paused"):
        shape = _shape_of(rules, (f".ctl-util-fill.{tone}",))
        assert "background-image" in shape, (
            f"`.ctl-util-fill.{tone}` carries no texture, so it is separated "
            f"from the other fills by hue alone"
        )
        patterns[tone] = shape["background-image"]

    duplicates = [
        f"{a} and {b}" for a, b in itertools.combinations(patterns, 2)
        if patterns[a] == patterns[b]
    ]
    assert not duplicates, (
        "two utilisation fills use the same texture: " + ", ".join(duplicates)
    )

    default = _shape_of(rules, (".ctl-util-fill",))
    assert "background-image" not in default, (
        "the default fill has a texture of its own, which leaves nothing for "
        "the toned fills to contrast against"
    )


def test_table_row_tones_carry_an_edge_rule(rules):
    """An 8% wash is about a 1% change in tone: in greyscale, no wash at all.

    Each toned row also carries a rule down the left edge of its first cell,
    and the three rules differ in form -- full height, half height, hatched --
    rather than only in colour.
    """
    rules_by_tone = {}
    for tone in ("is-bad", "is-warn", "is-paused"):
        selector = f".ctl-table tbody tr.{tone} > :first-child"
        shape = _shape_of(rules, (selector,))
        assert "background-image" in shape and "background-size" in shape, (
            f"`{selector}` draws no edge rule, so the row tone is an 8% wash "
            f"and nothing else"
        )
        rules_by_tone[tone] = (shape["background-image"], shape["background-size"])

    duplicates = [
        f"{a} and {b}" for a, b in itertools.combinations(rules_by_tone, 2)
        if rules_by_tone[a] == rules_by_tone[b]
    ]
    assert not duplicates, (
        "two table row tones draw the same edge rule: " + ", ".join(duplicates)
    )


# ---------------------------------------------------------------------------
# 3. One rule for every proportion fill: grey, unless it is saying something.
# ---------------------------------------------------------------------------
#
# OWNER DECISION, 2026-09-24 (design-system.md §6.4). `.ctl-util-fill`
# defaults to `--text-dim`. `.is-warn` / `.is-bad` / `.is-paused` keep their
# hue and their texture, because those three are a verdict. The Workflows
# meter carries `ctl-util-fill wf-meter-fill` and was exempted back to
# `--info` by one line until that decision. Colour on a bar is a verdict, and
# "three of five steps done" is not one.
#
# WHAT THE FIRST VERSION OF THIS GUARD GOT WRONG, so the next edit does not
# repeat it. It read `styles.css` only. It looked only at rules whose subject
# compound literally contained `.ctl-util-fill`. It counted a verdict class
# inside `:not()` as a verdict. Its docstring said a second exemption "fails
# here ... whatever it is named", and none of that held. Mutation commit
# 232921d painted the meter blue five ways, and that guard stayed green on all
# five (CI run 35977623224, `test_a_proportion_fill_takes_a_hue_only_from_a_
# verdict` passed):
#
#   .wf-meter .wf-meter-fill {...}      the fill's OTHER class: (0,2,0) beats
#                                       the default's (0,1,0) from anywhere
#   .wf-meter-fill {...}                equal specificity, later in the sheet
#   .ctl-util-fill.wf-x {...}           in OVERVIEW_CSS, the sheet Overview.tsx
#                                       injects after styles.css
#   .ctl-util-fill:not(.is-warn) {...}  a verdict NAMED, not carried
#   .wf-meter > span {...}              through the parent, no fill class
#
# (The same commit also put `background: 'var(--info)'` in the fill's inline
# style, which that guard had no way to see.)
#
# And it could not see the one exemption that already shipped:
# `.ctl-util-fill.ov-projected { background: var(--ctl-absent) }` in
# OVERVIEW_CSS. That is now listed BY NAME below as a documented grey.
#
# WHAT THIS VERSION ASKS INSTEAD: for each fill the product renders, which
# rules can paint it, and does a grey win? Concretely:
#
#   * THE SHEETS. `styles.css`, plus every sheet a `.tsx` injects as
#     `<style>{NAME}</style>`. A `<style>` whose text the scan cannot read
#     fails `test_the_fill_scan_reads_what_it_claims_to`, not silently.
#   * THE FILLS. Every intrinsic JSX element whose `className` names
#     `ctl-util-fill`. Each fill is tried bare and with every class it can
#     carry: the literals in its className, including both arms of a
#     conditional; the values of an identifier it interpolates, traced to that
#     file's assignments and JSX attributes of the same name (an identifier the
#     scan cannot trace fails the scan test); and every class any sheet names
#     beside `.ctl-util-fill`, so an exemption written for a class the scan
#     never saw is still tried.
#   * ITS PARENT, as the same JSX writes it: tag, classes, attributes.
#     Ancestors above the parent are unknown and are assumed to match
#     ANYTHING. So a rule scoped by a grandparent counts as a possible hit,
#     never as a miss.
#   * EVERY RULE THAT SETS `background`, `background-color` OR
#     `background-image`, at every breakpoint. Each is matched with real
#     selector semantics: type, class, id, attribute, `:not` / `:is` /
#     `:where`, and the child and descendant combinators. A dynamic
#     pseudo-class (`:hover`) counts as a possible hit.
#   * THE CASCADE. A rule that may paint an un-verdicted fill with anything
#     but its grey FAILS, unless some rule that certainly applies, paints the
#     grey and outranks it beats it: importance, then specificity, then
#     order, with injected sheets after styles.css. So the grouped
#     `.ctl-util-fill { background: var(--info) }` at the top of the track
#     block passes, because the default below it is equally specific and
#     later. The five above fail.
#   * An inline `style` that sets a background on a fill fails, because it
#     beats every sheet. A `{...spread}` on a fill fails the scan test, because
#     the attributes it carries cannot be read.
#
# WHAT IT STILL DOES NOT SEE. It reads source text; it does not render, so it
# says what the sheets would do, not what a browser painted.
#   * A class that reaches a fill through a function call (`cx(...)`, a helper
#     that returns a string) or through a prop set in another file is not
#     traced. A class that some sheet names beside `.ctl-util-fill` is still
#     tried on every fill.
#   * A fill whose className does not name `ctl-util-fill` literally is not
#     found.
#   * CSS injected any other way than `<style>{NAME}</style>` in a `.tsx` is
#     not read.
#   * Sibling combinators are always a possible hit, never a certain one.

UI_SRC = ROOT / "apps/swarm-ui/src"

#: The fills that carry a verdict, and so the only ones a hue is allowed on.
VERDICT_FILLS = frozenset({"is-warn", "is-bad", "is-paused"})

#: What a fill with nothing to report is painted in. design-system.md §6.4.
MONOCHROME_FILL = "var(--text-dim)"

#: The un-verdicted fills that are painted a DIFFERENT grey, by name, and why.
#:
#: `ov-projected`: Overview draws a PROJECTED utilisation reading (its window
#: reset, or its poll is past the staleness window) in `--ctl-absent`, the
#: grey this product uses for data that is real but not current. That grey
#: sits beside the live bars in the same card. It is still a grey and not a
#: hue, and `test_a_documented_grey_is_a_grey` resolves it to a text grey, so
#: the entry cannot become a hue with a grey name.
DOCUMENTED_GREYS = {"ov-projected": "var(--ctl-absent)"}

#: What a documented grey has to resolve to: the two text greys.
TEXT_GREYS = frozenset({"--text-dim", "--text-faint"})

#: Three-valued matching. MAYBE is what an unknown ancestor, an attribute
#: value or a `:hover` gives: the rule might reach, and a guard counts it.
NO, MAYBE, YES = 0, 1, 2
_NEGATE = {YES: NO, NO: YES, MAYBE: MAYBE}


@dataclass(frozen=True)
class _El:
    """What is known about one element on a fill's ancestor chain."""

    tag: str | None = None
    #: Classes it certainly carries.
    classes: frozenset[str] = frozenset()
    #: Classes it may carry (one arm of a conditional).
    maybe: frozenset[str] = frozenset()
    #: Attribute names, or None when a spread makes them unknowable.
    attrs: frozenset[str] | None = None
    #: A class came from an identifier the scan could not trace, so any
    #: class selector is a MAYBE rather than a NO.
    open_classes: bool = False
    known: bool = False


_UNKNOWN = _El()


# -- selectors ----------------------------------------------------------------

_IDENT_RE = re.compile(r"-?(?:[_a-zA-Z]|\\.)(?:[\w-]|\\.)*")
_LEGACY_PSEUDO_ELEMENTS = frozenset({"before", "after", "first-line", "first-letter"})


def _close(text: str, i: int, opener: str, closer: str) -> int:
    """Index of the `closer` matching the `opener` at `text[i]`."""
    depth, quote = 0, ""
    for j in range(i, len(text)):
        char = text[j]
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return j
    raise ValueError(f"unbalanced {opener!r} in {text!r}")


@functools.lru_cache(maxsize=None)
def _compounds(selector: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """`.a .b > i.c` -> (('.a', '.b', 'i.c'), (' ', '>'))."""
    compounds: list[str] = []
    combinators: list[str] = []
    buf: list[str] = []
    depth, quote, comb = 0, "", " "
    for char in selector.strip():
        if not quote and depth == 0 and (char.isspace() or char in ">+~"):
            if buf:
                compounds.append("".join(buf))
                buf = []
                comb = " "
            if char in ">+~":
                comb = char
            continue
        if not buf and compounds:
            combinators.append(comb)
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        buf.append(char)
    if buf:
        compounds.append("".join(buf))
    return tuple(compounds), tuple(combinators)


@functools.lru_cache(maxsize=None)
def _simples(compound: str) -> tuple[tuple[str, str, str | None], ...]:
    """One compound as (kind, name, argument) triples.

    kind is `type`, `class`, `id`, `attr` (name is the bracket's inside),
    `pseudo` or `pseudo-element`. Anything else raises ValueError, which the
    matcher turns into MAYBE: a selector this reader cannot parse is one it
    cannot clear.
    """
    out: list[tuple[str, str, str | None]] = []
    i = 0
    if compound[:1] == "*":
        out.append(("type", "*", None))
        i = 1
    elif compound[:1] not in (".", "#", ":", "["):
        match = _IDENT_RE.match(compound)
        if match is None:
            raise ValueError(compound)
        out.append(("type", match.group(0).lower(), None))
        i = match.end()
    while i < len(compound):
        char = compound[i]
        if char in ".#":
            match = _IDENT_RE.match(compound, i + 1)
            if match is None:
                raise ValueError(compound)
            out.append(("class" if char == "." else "id", match.group(0), None))
            i = match.end()
        elif char == "[":
            j = _close(compound, i, "[", "]")
            out.append(("attr", compound[i + 1:j], None))
            i = j + 1
        elif char == ":":
            double = compound.startswith("::", i)
            match = _IDENT_RE.match(compound, i + (2 if double else 1))
            if match is None:
                raise ValueError(compound)
            name, i = match.group(0).lower(), match.end()
            arg = None
            if i < len(compound) and compound[i] == "(":
                j = _close(compound, i, "(", ")")
                arg, i = compound[i + 1:j], j + 1
            kind = "pseudo-element" if double or name in _LEGACY_PSEUDO_ELEMENTS else "pseudo"
            out.append((kind, name, arg))
        else:
            raise ValueError(compound)
    return tuple(out)


def _args(arg: str | None) -> list[str]:
    return [s for s in _split_top(arg or "") if s.strip()]


@functools.lru_cache(maxsize=None)
def _specificity(selector: str) -> tuple[int, int, int]:
    """(ids, classes + attributes + pseudo-classes, types + pseudo-elements).

    `:not()` / `:is()` / `:has()` count as their most specific argument and
    `:where()` counts nothing, per Selectors Level 4.
    """
    a = b = c = 0
    for compound in _compounds(selector)[0]:
        for kind, name, arg in _simples(compound):
            if kind == "id":
                a += 1
            elif kind in ("class", "attr"):
                b += 1
            elif kind == "type":
                c += 0 if name == "*" else 1
            elif kind == "pseudo-element":
                c += 1
            elif name == "where":
                continue
            elif name in ("not", "is", "matches", "-webkit-any", "has"):
                inner = max((_specificity(s) for s in _args(arg)), default=(0, 0, 0))
                a, b, c = a + inner[0], b + inner[1], c + inner[2]
            else:
                b += 1
    return a, b, c


def _match_compound(simples, chain: tuple[_El, ...], i: int) -> int:
    el = chain[i] if i < len(chain) else _UNKNOWN
    if not el.known:
        return MAYBE
    result = YES
    for kind, name, arg in simples:
        if kind == "type":
            if name != "*" and name != el.tag:
                return NO
        elif kind == "class":
            if name in el.maybe:
                result = min(result, MAYBE)
            elif name not in el.classes:
                if not el.open_classes:
                    return NO
                result = min(result, MAYBE)
        elif kind in ("id", "attr"):
            attr = "id" if kind == "id" else re.match(r"\s*([\w-]*)", name).group(1).lower()
            if el.attrs is not None and attr not in el.attrs:
                return NO
            bare = kind == "attr" and re.fullmatch(r"\s*[\w-]+\s*", name) is not None
            result = min(result, YES if bare and el.attrs is not None else MAYBE)
        elif kind == "pseudo-element":
            # `::before` / `::after` paint a box of their own, not the fill.
            return NO
        elif name in ("not", "is", "where", "matches", "-webkit-any"):
            inner = max((_match_complex(s, chain, i) for s in _args(arg)), default=NO)
            result = min(result, _NEGATE[inner] if name == "not" else inner)
        elif name == "root":
            if el.tag != "html":
                return NO
        else:
            result = min(result, MAYBE)
        if result == NO:
            return NO
    return result


def _match_from(parsed, combinators, k: int, chain: tuple[_El, ...], i: int) -> int:
    matched = _match_compound(parsed[k], chain, i)
    if matched == NO or k == 0:
        return matched
    comb = combinators[k - 1]
    if comb == ">":
        return min(matched, _match_from(parsed, combinators, k - 1, chain, i + 1))
    if comb == " ":
        best, j = NO, i + 1
        while True:
            best = max(best, _match_from(parsed, combinators, k - 1, chain, j))
            if best == YES or j >= len(chain) or not chain[j].known:
                break
            j += 1
        return min(matched, best)
    return min(matched, MAYBE)  # `+` and `~`: siblings are not modelled


@functools.lru_cache(maxsize=None)
def _match_complex(selector: str, chain: tuple[_El, ...], i: int = 0) -> int:
    """Can `selector` match `chain[i]`, whose ancestors are `chain[i + 1:]`?"""
    try:
        compounds, combinators = _compounds(selector)
        parsed = [_simples(c) for c in compounds]
    except ValueError:
        return MAYBE
    if not parsed:
        return NO
    return _match_from(parsed, combinators, len(parsed) - 1, chain, i)


# -- the sheets ---------------------------------------------------------------

@dataclass(frozen=True)
class _Paint:
    """One rule's effect on one background longhand."""

    sheet: str
    order: int
    at_rules: str
    selector: str
    specificity: tuple[int, int, int]
    longhand: str
    source: str
    value: str
    important: bool

    def grey(self, allowed: frozenset[str]) -> bool:
        if self.source == "background-image":
            return self.value in ("none", "initial", "unset")
        return self.value in allowed


@dataclass(frozen=True)
class _SheetScan:
    sheets: tuple[tuple[str, tuple[Rule, ...]], ...]
    unreadable: tuple[str, ...]


def _ui_sources() -> list[Path]:
    return sorted(p for p in UI_SRC.rglob("*.tsx") if "__tests__" not in p.parts)


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _line(code: str, index: int) -> int:
    return code.count("\n", 0, index) + 1


def _read_sheets() -> _SheetScan:
    sheets = [(_rel(CSS_PATH), tuple(parse_rules(CSS_PATH.read_text(encoding="utf-8"))))]
    unreadable: list[str] = []
    for path in _ui_sources():
        src = path.read_text(encoding="utf-8")
        code = _blank_comments(src)
        for tag in re.finditer(r"<style\b", code):
            where = f"{_rel(path)}:{_line(code, tag.start())}"
            named = re.compile(r"<style>\s*\{\s*([A-Za-z_$][\w$]*)\s*\}\s*</style>").match(
                code, tag.start())
            decl = named and re.search(
                r"(?<![\w$.])(?:const|let|var)\s+" + re.escape(named.group(1)) + r"\s*=\s*`", code)
            if not decl:
                unreadable.append(f"{where}: not `<style>{{NAME}}</style>` over a template literal")
                continue
            start = decl.end() - 1
            text = src[start + 1:_template_end(src, start) - 1]
            if "${" in text:
                unreadable.append(f"{where}: `{named.group(1)}` interpolates")
                continue
            sheets.append((f"{_rel(path)} {named.group(1)}", tuple(parse_rules(text))))
    return _SheetScan(tuple(sheets), tuple(unreadable))


def _paints(sheets) -> list[_Paint]:
    out: list[_Paint] = []
    order = 0
    for name, rules_ in sheets:
        for at_rules, selector, decls in rules_:
            order += 1
            if "@keyframes" in at_rules or "@font-face" in at_rules:
                continue
            for prop in ("background", "background-color", "background-image"):
                if prop not in decls:
                    continue
                try:
                    spec = _specificity(selector)
                except ValueError:
                    spec = (9, 9, 9)  # unreadable: nothing may be assumed to outrank it
                raw = decls[prop]
                important = raw.endswith("!important")
                value = raw[: -len("!important")].strip() if important else raw
                longhands = (("background-color", "background-image")
                             if prop == "background" else (prop,))
                for longhand in longhands:
                    out.append(_Paint(name, order, at_rules, selector, spec,
                                      longhand, prop, value, important))
    return out


def _companions(sheets) -> set[frozenset[str]]:
    """Every class set a sheet names beside `.ctl-util-fill` in a subject."""
    out: set[frozenset[str]] = set()
    for _, rules_ in sheets:
        for _, selector, _ in rules_:
            try:
                compounds, _ = _compounds(selector)
                simples = _simples(compounds[-1]) if compounds else ()
            except ValueError:
                continue
            classes = {name for kind, name, _ in simples if kind == "class"}
            if "ctl-util-fill" in classes and len(classes) > 1:
                out.add(frozenset(classes - {"ctl-util-fill"}))
    return out


# -- the fills, read out of the .tsx ------------------------------------------
#
# A small, deliberately partial reader of JSX. It only has to find opening
# tags, their attributes and their parents. It is not a parser, and each
# place it has to guess is written as MAYBE in the matcher or as a failure in
# the scan test, never as a quiet NO.

_HTML = frozenset("""
    a abbr article aside b bdi blockquote br button caption cite code col colgroup
    data dd del details dfn dialog div dl dt em fieldset figcaption figure footer
    form h1 h2 h3 h4 h5 h6 header hr i img input ins kbd label legend li main mark
    menu meter nav ol optgroup option output p picture pre progress q s samp section
    select small span strong style sub summary sup table tbody td template textarea
    tfoot th thead time tr u ul var video wbr svg g line rect circle ellipse path
    polygon polyline text tspan title defs pattern clippath mask lineargradient
    radialgradient stop use foreignobject
""".split())

_OPEN_RE = re.compile(r"<([A-Za-z][\w.]*)?")
_CLOSE_RE = re.compile(r"</([A-Za-z][\w.]*)?\s*>")
_CLASS_TOKEN = re.compile(r"-?[_a-zA-Z][\w-]*")
_BARE = re.compile(r"[A-Za-z_$][\w$]*")


def _blank_comments(src: str) -> str:
    """The source with its comments turned to spaces, newlines kept.

    Offsets and line numbers still point into the original, and a `<b>` or a
    `className=` quoted in a comment is not read as code. A comment in this
    codebase quotes the code it explains more often than not.
    """
    def spaces(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    src = re.sub(r"/\*.*?\*/", spaces, src, flags=re.DOTALL)
    return re.sub(r"(?m)^[ \t]*//[^\n]*", spaces, src)


def _string_end(s: str, i: int) -> int:
    """`s[i]` opens a '...' or "..." string: the index just past its close.

    A newline ends it early. JS strings cannot span lines, so reaching one
    means the quote was an apostrophe in JSX text, and stopping there limits
    the damage to one line.
    """
    quote, j = s[i], i + 1
    while j < len(s):
        if s[j] == "\\":
            j += 2
            continue
        if s[j] == quote:
            return j + 1
        if s[j] == "\n":
            return j
        j += 1
    raise ValueError("unterminated string")


def _template_end(s: str, i: int) -> int:
    """`s[i]` is a backtick: the index just past the template's closing one."""
    j = i + 1
    while j < len(s):
        char = s[j]
        if char == "\\":
            j += 2
            continue
        if char == "`":
            return j + 1
        if s.startswith("${", j):
            j = _expr_end(s, j + 1)
            continue
        j += 1
    raise ValueError("unterminated template literal")


def _expr_end(s: str, i: int) -> int:
    """`s[i]` is `{`: the index just past its matching `}`."""
    depth, j = 0, i
    while j < len(s):
        char = s[j]
        if char in "'\"":
            j = _string_end(s, j)
            continue
        if char == "`":
            j = _template_end(s, j)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise ValueError("unbalanced braces")


def _tag_end(s: str, i: int) -> tuple[int, bool]:
    """`s[i]` is the `<` of an opening tag: (index past its `>`, self-closing?)."""
    j = i + 1
    while j < len(s):
        char = s[j]
        if char in "'\"":
            j = _string_end(s, j)
            continue
        if char == "{":
            j = _expr_end(s, j)
            continue
        if char == ">":
            return j + 1, s[j - 1] == "/"
        j += 1
    raise ValueError("unterminated tag")


def _shallow(tag: str) -> str:
    """The tag with the insides of its strings and `{...}` blanked out."""
    out, j = list(tag), 1
    while j < len(tag):
        char = tag[j]
        if char in "'\"":
            end = _string_end(tag, j)
        elif char == "{":
            end = _expr_end(tag, j)
        else:
            j += 1
            continue
        for k in range(j + 1, end - 1):
            out[k] = " "
        j = end
    return "".join(out)


def _attr_value(tag: str, attr: str) -> str | None:
    """The raw value of one attribute: `"..."` or `{...}`, delimiters kept."""
    match = re.search(r"(?<=\s)" + attr + r"\s*=\s*", _shallow(tag))
    if match is None:
        return None
    j = match.end()
    if tag[j:j + 1] in ("'", '"'):
        return tag[j:_string_end(tag, j)]
    if tag[j:j + 1] == "{":
        return tag[j:_expr_end(tag, j)]
    return None


def _attr_names(tag: str) -> frozenset[str] | None:
    """The attributes a tag writes, or None when a `{...spread}` hides some."""
    shallow = _shallow(tag)
    for brace in re.finditer(r"\{", shallow):
        if not re.search(r"=\s*$", shallow[:brace.start()]) and tag[brace.end():].lstrip().startswith("..."):
            return None
    names = re.findall(r"(?<=\s)([A-Za-z_][\w:.-]*)(?=\s*=|\s|/?>)", shallow)
    rename = {"classname": "class", "htmlfor": "for"}
    return frozenset(rename.get(n.lower(), n.lower()) for n in names)


def _tokens(text: str) -> set[str]:
    return {t for t in text.split() if _CLASS_TOKEN.fullmatch(t)}


def _template_parts(t: str) -> tuple[list[str], list[str]]:
    """A whole template literal as (literal text parts, `${...}` expressions)."""
    texts, exprs, buf = [], [], []
    j = 1
    while j < len(t) - 1:
        if t[j] == "\\":
            buf.append(t[j:j + 2])
            j += 2
            continue
        if t.startswith("${", j):
            end = _expr_end(t, j + 1)
            texts.append("".join(buf))
            buf = []
            exprs.append(t[j + 2:end - 1])
            j = end
            continue
        buf.append(t[j])
        j += 1
    texts.append("".join(buf))
    return texts, exprs


def _expr_sources(expr: str) -> tuple[set[str], set[str], int]:
    """Class tokens in an expression's string literals, the bare identifiers
    it interpolates, and how many literals it had."""
    expr = expr.strip()
    if _BARE.fullmatch(expr) and expr not in ("true", "false", "null", "undefined"):
        return set(), {expr}, 0
    tokens: set[str] = set()
    idents: set[str] = set()
    count, j = 0, 0
    while j < len(expr):
        char = expr[j]
        if char in "'\"":
            end = _string_end(expr, j)
            tokens |= _tokens(expr[j + 1:end - 1])
            count, j = count + 1, end
            continue
        if char == "`":
            end = _template_end(expr, j)
            texts, exprs = _template_parts(expr[j:end])
            for text in texts:
                tokens |= _tokens(text)
            count += 1
            for inner in exprs:
                t2, i2, n2 = _expr_sources(inner)
                tokens, idents, count = tokens | t2, idents | i2, count + n2
            j = end
            continue
        j += 1
    return tokens, idents, count


def _class_sources(value: str) -> tuple[set[str], set[str], set[str]]:
    """A className value as (certain classes, possible classes, identifiers)."""
    v = value.strip()
    if v[:1] in ("'", '"'):
        return _tokens(v[1:-1]), set(), set()
    inner = re.sub(r"\s*\.trim(?:Start|End)?\(\)\s*$", "", v[1:-1].strip())
    if inner[:1] in ("'", '"') and _string_end(inner, 0) == len(inner):
        return _tokens(inner[1:-1]), set(), set()
    if inner[:1] == "`" and _template_end(inner, 0) == len(inner):
        texts, exprs = _template_parts(inner)
        certain = set().union(*(_tokens(t) for t in texts))
        possible: set[str] = set()
        idents: set[str] = set()
        for expr in exprs:
            t2, i2, _ = _expr_sources(expr)
            possible, idents = possible | t2, idents | i2
        return certain, possible, idents
    tokens, idents, _ = _expr_sources(inner)
    return set(), tokens, idents


def _statement(code: str, j: int) -> str:
    """The right-hand side of an assignment starting at `code[j]`."""
    k, depth = j, 0
    while k < len(code):
        char = code[k]
        if char in "'\"":
            k = _string_end(code, k)
            continue
        if char == "`":
            k = _template_end(code, k)
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif char == ";" and depth == 0:
            break
        elif char == "\n" and depth == 0:
            rest = code[k + 1:].lstrip()
            prev = code[j:k].rstrip()
            carries = (rest[:1] in ("?", ":", ".", "+")
                       or rest.startswith(("&&", "||", "??"))
                       or prev.endswith(("?", ":", "&&", "||", "??", "+", "=", "(", ",")))
            if not carries:
                break
        k += 1
    return code[j:k]


def _resolve(name: str, code: str, seen: frozenset[str] = frozenset()) -> tuple[set[str], bool]:
    """Every class token `name` can hold in this file, and whether any
    assignment or JSX attribute of that name was found at all."""
    if name in seen:
        return set(), False
    seen = seen | {name}
    tokens: set[str] = set()
    found = False
    for match in re.finditer(r"(?<![\w$.])" + re.escape(name) + r"\s*=(?![=>])\s*", code):
        j = match.end()
        try:
            expr = code[j + 1:_expr_end(code, j) - 1] if code[j:j + 1] == "{" else _statement(code, j)
        except ValueError:
            continue
        t2, idents, count = _expr_sources(expr)
        tokens, found = tokens | t2, found or count > 0
        for ident in idents:
            t3, f3 = _resolve(ident, code, seen)
            tokens, found = tokens | t3, found or f3
    return tokens, found


@dataclass(frozen=True)
class _Tok:
    kind: str  # "open" | "close" | "fragment"
    name: str
    start: int


def _jsx_tags(code: str) -> list[_Tok]:
    """Opening, closing and fragment tokens, in source order.

    An opening `<x` glued to the end of a word (`Array<string>`,
    `useState<Me>`) is a TypeScript generic unless `x` is an HTML or SVG
    element name.
    """
    toks: list[_Tok] = []
    for m in _OPEN_RE.finditer(code):
        name = m.group(1) or ""
        before = code[m.start() - 1] if m.start() else ""
        glued = bool(before) and (before.isalnum() or before in "_$.)]")
        nxt = code[m.end():m.end() + 1]
        if not name:
            if nxt == ">" and not glued:
                toks.append(_Tok("fragment", "", m.start()))
            continue
        if not nxt or not (nxt.isspace() or nxt in "/>"):
            continue
        if glued and not (name[:1].islower() and name.lower() in _HTML):
            continue
        toks.append(_Tok("open", name, m.start()))
    for m in _CLOSE_RE.finditer(code):
        toks.append(_Tok("close", m.group(1) or "", m.start()))
    return sorted(toks, key=lambda t: t.start)


def _is_intrinsic(name: str) -> bool:
    return name[:1].islower() and "." not in name


def _element(code: str, tok: _Tok, tag: str) -> tuple[_El, list[str]]:
    """The `_El` a JSX opening tag describes, and any identifier its
    className interpolates that the scan could not trace."""
    value = _attr_value(tag, "className")
    certain, possible, idents = _class_sources(value) if value else (set(), set(), set())
    untraced = []
    for ident in sorted(idents):
        tokens, found = _resolve(ident, code)
        possible |= tokens
        if not found:
            untraced.append(ident)
    return _El(tag=tok.name.lower(), classes=frozenset(certain),
               maybe=frozenset(possible - certain), attrs=_attr_names(tag),
               open_classes=bool(untraced), known=True), untraced


def _parent(code: str, before: list[_Tok]) -> _El:
    """The element the next tag is a child of, walking back over siblings."""
    depth = 0
    for tok in reversed(before):
        if tok.kind == "close":
            depth += 1
            continue
        end = tok.start
        if tok.kind == "open":
            try:
                end, self_closing = _tag_end(code, tok.start)
            except ValueError:
                continue
            if self_closing:
                continue
        if depth:
            depth -= 1
            continue
        if tok.kind == "fragment":
            continue  # a fragment renders no element: its parent is further up
        if not _is_intrinsic(tok.name):
            return _UNKNOWN  # a component: what it renders around us is not here
        try:
            return _element(code, tok, code[tok.start:end])[0]
        except ValueError:
            return _UNKNOWN  # unreadable: assumed to match anything
    return _UNKNOWN


@dataclass(frozen=True)
class _Site:
    where: str
    tag: str
    certain: frozenset[str]
    possible: frozenset[str]
    attrs: frozenset[str] | None
    open_classes: bool
    inline_paint: bool
    parent: _El


@dataclass(frozen=True)
class _FillScan:
    sites: tuple[_Site, ...]
    untraced: tuple[str, ...]
    opaque: tuple[str, ...]


def _scan_fills() -> _FillScan:
    sites: list[_Site] = []
    untraced: list[str] = []
    opaque: list[str] = []
    for path in _ui_sources():
        code = _blank_comments(path.read_text(encoding="utf-8"))
        toks = _jsx_tags(code)
        for n, tok in enumerate(toks):
            if tok.kind != "open" or not _is_intrinsic(tok.name):
                continue
            try:
                end, _ = _tag_end(code, tok.start)
            except ValueError:
                continue
            tag = code[tok.start:end]
            if "ctl-util-fill" not in tag:
                continue
            where = f"{_rel(path)}:{_line(code, tok.start)}"
            try:
                el, missing = _element(code, tok, tag)
            except ValueError as exc:
                opaque.append(f"{where}: its tag could not be read ({exc})")
                continue
            if "ctl-util-fill" not in el.classes | el.maybe:
                continue
            untraced += [f"{where} `{ident}`" for ident in missing]
            if el.attrs is None:
                opaque.append(where)
            style = _attr_value(tag, "style") or ""
            sites.append(_Site(
                where=where,
                tag=el.tag,
                certain=el.classes | {"ctl-util-fill"},
                possible=el.maybe - {"ctl-util-fill"},
                attrs=el.attrs,
                open_classes=el.open_classes,
                inline_paint=re.search(r"\bbackground(?:Color|Image)?\s*:", style) is not None,
                parent=_parent(code, toks[:n]),
            ))
    return _FillScan(tuple(sites), tuple(untraced), tuple(opaque))


def _variants(site: _Site, companions: set[frozenset[str]]) -> list[frozenset[str]]:
    """The un-verdicted class sets this fill can be drawn with."""
    out = ({site.certain}
           | {site.certain | {c} for c in site.possible}
           | {site.certain | c for c in companions})
    return sorted((v for v in out if not v & VERDICT_FILLS), key=sorted)


def _unbeaten(site: _Site, variant: frozenset[str], paints: list[_Paint],
              allowed: frozenset[str]) -> list[tuple[_Paint, int]]:
    fill = _El(tag=site.tag, classes=variant, attrs=site.attrs,
               open_classes=site.open_classes, known=True)
    chain = (fill, site.parent)
    reach = [(p, m) for p in paints if (m := _match_complex(p.selector, chain)) != NO]
    out = []
    for p, m in reach:
        if p.grey(allowed):
            continue
        beaten = any(
            q.longhand == p.longhand and qm == YES and q.grey(allowed)
            and q.at_rules in ("", p.at_rules)
            and (q.important, q.specificity, q.order) > (p.important, p.specificity, p.order)
            for q, qm in reach
        )
        if not beaten:
            out.append((p, m))
    return out


@pytest.fixture(scope="module")
def sheet_scan() -> _SheetScan:
    return _read_sheets()


@pytest.fixture(scope="module")
def fill_scan() -> _FillScan:
    return _scan_fills()


def test_a_proportion_fill_takes_a_hue_only_from_a_verdict(rules, sheet_scan, fill_scan):
    """Every fill the product renders is grey unless it carries a verdict.

    See the block comment above this section for exactly what is read and
    what is not. In one line: for each `.ctl-util-fill` in the `.tsx`, bare
    and with each class it can carry, every rule in every sheet that can
    reach it is matched, and a rule that could paint it anything but its grey
    must be outranked by a grey rule that certainly applies.
    """
    default = None
    for at_rules, selector, decls in rules:
        if at_rules or selector != ".ctl-util-fill":
            continue
        for prop in ("background", "background-color"):
            if prop in decls:
                default = decls[prop]
    assert default == MONOCHROME_FILL, (
        f"the bare `.ctl-util-fill` resolves to {default!r}, not "
        f"{MONOCHROME_FILL}: a bar with nothing to report is hued again"
    )

    paints = _paints(sheet_scan.sheets)
    companions = _companions(sheet_scan.sheets)
    problems: set[str] = set()
    tried = 0
    for site in fill_scan.sites:
        if site.inline_paint:
            problems.add(f"{site.where}: an inline `style` paints the fill, and "
                         f"inline beats every rule in every sheet")
        for variant in _variants(site, companions):
            tried += 1
            allowed = frozenset({MONOCHROME_FILL} | {
                DOCUMENTED_GREYS[c] for c in variant if c in DOCUMENTED_GREYS})
            for paint, reach in _unbeaten(site, variant, paints, allowed):
                how = "reaches" if reach == YES else "may reach"
                inside = f" inside {paint.at_rules}" if paint.at_rules else ""
                problems.add(
                    f"{site.where} <{site.tag} class=\"{' '.join(sorted(variant))}\">: "
                    f"`{paint.selector} {{ {paint.source}: {paint.value} }}` in "
                    f"{paint.sheet}{inside} {how} it, and no grey rule that "
                    f"certainly applies outranks it"
                )
    assert not problems, (
        "a proportion fill that carries no verdict is painted something other "
        f"than grey (design-system.md §6.4, decided 2026-09-24). Tried "
        f"{tried} class sets on {len(fill_scan.sites)} fills against "
        f"{len(paints)} background declarations in {len(sheet_scan.sheets)} "
        "sheets:\n  " + "\n  ".join(sorted(problems))
    )


def test_the_fill_scan_reads_what_it_claims_to(sheet_scan, fill_scan):
    """EMPTY OUTPUT IS NOT SUCCESS. The guard above is only as wide as what it read.

    Each thing the scan could not read makes it narrower without making it
    fail, so each one fails here instead: a `<style>` whose sheet it cannot
    find, an identifier in a fill's className it cannot trace, a spread on a
    fill. And the fill the owner's decision was about has to be among what it
    found, with the parent it really has. That is what catches the reader
    itself breaking.
    """
    assert not sheet_scan.unreadable, (
        "a screen injects CSS the fill guard cannot read, so an exemption "
        "there would pass unseen:\n  " + "\n  ".join(sheet_scan.unreadable)
    )
    assert not fill_scan.untraced, (
        "a fill's className interpolates an identifier the scan cannot trace "
        "to any assignment or JSX attribute in its file, so the classes it "
        "can carry are unknown:\n  " + "\n  ".join(fill_scan.untraced)
    )
    assert not fill_scan.opaque, (
        "a fill carries a `{...spread}`, so its attributes (and any inline "
        "style) cannot be read:\n  " + "\n  ".join(fill_scan.opaque)
    )
    assert fill_scan.sites, (
        "the scan found no element naming `ctl-util-fill` in any .tsx: it "
        "read nothing, so the guard above proved nothing"
    )
    meters = [s for s in fill_scan.sites if "wf-meter-fill" in s.certain]
    found = "\n  ".join(
        f"{s.where} <{s.tag} {' '.join(sorted(s.certain))}> in "
        f"<{s.parent.tag} {' '.join(sorted(s.parent.classes))}>"
        for s in fill_scan.sites)
    assert meters, (
        "the Workflows meter's fill (`ctl-util-fill wf-meter-fill`), the one "
        "the 2026-09-24 decision is about, was not found. Fills found:\n  " + found
    )
    assert all({"ctl-track", "wf-meter"} <= s.parent.classes for s in meters), (
        "the Workflows meter's fill was found but its parent was not read as "
        "`.ctl-track.wf-meter`, so a rule reaching it through the parent would "
        "be missed. Fills found:\n  " + found
    )


def _token_ends(value: str, custom: dict[str, set[str]], depth: int = 0) -> set[str]:
    match = re.fullmatch(r"var\((--[\w-]+)\)", value.strip())
    if match is None:
        return {value}
    name = match.group(1)
    if name in TEXT_GREYS or depth > 8 or name not in custom:
        return {name}
    return set().union(*(_token_ends(v, custom, depth + 1) for v in custom[name]))


def test_a_documented_grey_is_a_grey(sheet_scan):
    """`DOCUMENTED_GREYS` may name a different grey, never a hue.

    Each entry must resolve, through every definition of every custom
    property on the way, to one of the two text greys. Re-pointing
    `--ctl-absent` at `--info` would otherwise make the one named exception
    a blue bar that this file waves through.
    """
    custom: dict[str, set[str]] = {}
    for _, rules_ in sheet_scan.sheets:
        for _, _, decls in rules_:
            for prop, value in decls.items():
                if prop.startswith("--"):
                    custom.setdefault(prop, set()).add(value)
    hued = {
        cls: sorted(_token_ends(value, custom))
        for cls, value in DOCUMENTED_GREYS.items()
        if not _token_ends(value, custom) <= TEXT_GREYS
    }
    assert not hued, (
        f"a documented grey resolves to something other than {sorted(TEXT_GREYS)}: {hued}"
    )
