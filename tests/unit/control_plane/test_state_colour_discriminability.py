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

import itertools
import re
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


def parse_rules(css: str) -> list[Rule]:
    """Every rule in the sheet, as (enclosing at-rules, one selector, decls).

    A comma-separated selector list is expanded into one entry per selector,
    because that is how the assertions below want to ask the question.
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
                for selector in prelude.split(","):
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


#: The fills that carry a verdict, and so the only ones a hue is allowed on.
VERDICT_FILLS = frozenset({"is-warn", "is-bad", "is-paused"})

#: What a fill with nothing to report is painted in. design-system.md §6.4.
MONOCHROME_FILL = "var(--text-dim)"


def _subject_classes(selector: str) -> set[str]:
    """The classes on the element a selector actually paints.

    The last compound, after any descendant or child combinator: in
    `.pool .ctl-util-fill.x` that is `{"ctl-util-fill", "x"}`, and `.pool` is
    only where it is.
    """
    subject = re.split(r"\s*[>+~]\s*|\s+", selector.strip())[-1]
    return set(re.findall(r"\.([A-Za-z0-9_-]+)", subject))


def test_a_proportion_fill_takes_a_hue_only_from_a_verdict(rules):
    """One rule for every proportion fill: grey, unless it is saying something.

    OWNER DECISION, 2026-09-24 (design-system.md §6.4). `.ctl-util-fill`
    defaults to `--text-dim`, and `.is-warn` / `.is-bad` / `.is-paused` keep
    their hue and their texture because those three are a verdict. The
    Workflows meter carries `ctl-util-fill wf-meter-fill` and was exempted back
    to `--info` by one line, held open as a question because Workflows had
    been frozen. The answer: it goes grey like every other proportion. Colour
    on a bar is a verdict, and "three of five steps done" is not one.

    THE PROPERTY, NOT THE SELECTOR. This does not look for `wf-meter-fill` by
    name. Any rule whose subject is a `.ctl-util-fill` -- whatever screen class
    rides along with it, under whatever ancestor, at whatever breakpoint --
    and which carries no verdict class may not paint a background other than
    the monochrome default. A second exemption under a new name fails here
    exactly as the old one does.
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

    exemptions = []
    for at_rules, selector, decls in rules:
        classes = _subject_classes(selector)
        if "ctl-util-fill" not in classes or classes & VERDICT_FILLS:
            continue
        if not at_rules and selector == ".ctl-util-fill":
            continue  # the default itself, cascaded above
        for prop in ("background", "background-color"):
            if prop in decls and decls[prop] != MONOCHROME_FILL:
                where = f" inside {at_rules}" if at_rules else ""
                exemptions.append(f"`{selector} {{ {prop}: {decls[prop]} }}`{where}")
    assert not exemptions, (
        "a proportion fill that carries no verdict is painted a hue, so one "
        "bar in the product is coloured for being a bar (design-system.md "
        "§6.4, decided 2026-09-24):\n  " + "\n  ".join(exemptions)
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
