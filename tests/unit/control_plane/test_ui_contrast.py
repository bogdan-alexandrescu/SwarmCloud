"""Text in this console has to be readable, in both themes, measurably.

THE DEFECT. Ten chips in styles.css set `color: var(--X)` on a background mixed
from `var(--X)` -- the state words (failed, running, succeeded, paused, over
quota) and the two header bars. Accent on a tint of the same accent: the tint
spends the margin the accent had against the plain surface, and every one of
those pairs landed under the 4.5:1 WCAG AA floor for normal text. Light mode was
the worse half -- 3.45:1 for the blue badge, 3.48:1 for the purple one -- and
all of them are set at 10-11.5px, where the large-text exemption does not apply.
One plain token failed too: light-mode `--warn` measured 4.37:1 against
`--surface-2`, the striped-row and panel-header background, which is where the
warning words actually sit.

WHY A TEST AND NOT A ONE-OFF SWEEP. A colour is picked by eye and a ratio is
not visible to the eye that picked it. Every one of these pairs was introduced
by someone reasonable, and the two `--text-faint` corrections already recorded
in styles.css show the same thing being found twice by hand. So this computes
the ratio from the shipped stylesheet on every run.

IT DISCOVERS THE PAIRS, it does not list them. Any rule that sets both a
`color` and a `background` is measured, so a chip added next month is covered
the moment it is written rather than when somebody next audits. A background
this cannot resolve -- a gradient, an image -- is NOT skipped: it has to be
named in UNRESOLVED_BACKGROUNDS below, so an unreadable new one fails here
instead of disappearing from the run. A silent skip is indistinguishable from a
pass, which is the failure mode this repository keeps producing.

WHAT IT CANNOT SEE. A rule that sets `color` and inherits its background from
an ancestor; this file has no DOM to walk. `test_every_accent_is_readable_as_plain_text`
covers the common case of that -- an accent used as bare text on each of the
three surfaces -- which is the check that caught `--warn`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
CSS = REPO / "apps/swarm-ui/src/styles.css"

#: WCAG 2.1 AA for normal text. Not 3:1: the large-text exemption needs 18.66px
#: bold or 24px regular, and the chips this guards are 10-11.5px.
AA = 4.5

#: Backgrounds no ratio can be computed from, named so that a new one is a
#: failure rather than a quiet skip. Both are deliberate: the hatch says "we
#: could not read whether this is paused", and its text is --text-dim over
#: surfaces that both clear AA on their own.
UNRESOLVED_BACKGROUNDS = (
    "repeating-linear-gradient",
    "var(--ctl-hatch)",
)

#: The accents, and the surfaces they are set as bare text on.
ACCENTS = ("--ok", "--warn", "--bad", "--info", "--paused", "--text", "--text-dim", "--text-faint")
SURFACES = ("--bg", "--surface", "--surface-2")


# --------------------------------------------------------------------------
# Colour arithmetic. sRGB relative luminance, WCAG 2.x definition.


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _rgb(colour: str) -> tuple[int, int, int]:
    h = colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    assert re.fullmatch(r"[0-9a-fA-F]{6}", h), f"{colour!r} is not a hex colour"
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def luminance(colour: str) -> float:
    r, g, b = _rgb(colour)
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(fg: str, bg: str) -> float:
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _blend(fg: str, bg: str, weight: float) -> str:
    fr, fg_, fb = _rgb(fg)
    br, bg_, bb = _rgb(bg)
    return "#%02x%02x%02x" % (
        round(fr * weight + br * (1 - weight)),
        round(fg_ * weight + bg_ * (1 - weight)),
        round(fb * weight + bb * (1 - weight)),
    )


# --------------------------------------------------------------------------
# The stylesheet


def _strip_comments(css: str) -> str:
    """Drop `/* ... */` before anything else reads the text.

    Not optional. The house style in styles.css is a long comment beside any
    value that was argued over, and those comments quote token names and
    ratios -- `--surface-2: 4.86:1` inside a comment parses as a declaration
    of `--surface-2` and shadows the real one. This test was written without
    this step and the very comment explaining the fix silently deleted
    `--surface-2` from the palette it was checking.
    """
    return re.sub(r"/\*.*?\*/", " ", css, flags=re.S)


def _blocks(css: str) -> list[tuple[str, str]]:
    """Every innermost rule, as (selector text, declarations).

    The body pattern excludes braces, so an `@media` wrapper never matches as
    a block -- the rules inside it do, carrying the media prelude in their
    selector text, which is all this needs.
    """
    return re.findall(r"([^{}]+)\{([^{}]*)\}", css)


def _tokens(css: str) -> tuple[dict[str, str], dict[str, str]]:
    """The custom properties of each theme, as {name: literal hex}.

    Both are declared more than once -- the base palette and the later
    control-room block -- so every matching rule is merged rather than the
    first one taken.
    """
    dark: dict[str, str] = {}
    light: dict[str, str] = {}
    for selector, body in _blocks(css):
        sel = " ".join(selector.split())
        raw = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body))
        if sel.endswith(":root"):
            dark.update(raw)
        elif ":root:not([data-theme='dark'])" in sel:
            light.update(raw)
    assert dark, "no :root custom properties found in styles.css"
    assert light, "no light-theme custom-property overrides found in styles.css"
    # Light mode only overrides part of the palette; everything else is
    # inherited from the dark :root, exactly as the cascade does it.
    merged_light = {**dark, **light}
    return _resolve_all(dark), _resolve_all(merged_light)


def _resolve_all(raw: dict[str, str]) -> dict[str, str]:
    """Follow `var()` aliases until every token is a literal, or drop it."""
    out: dict[str, str] = {}
    for name in raw:
        value, seen = raw[name].strip(), 0
        while value.startswith("var(") and seen < 8:
            inner = re.fullmatch(r"var\((--[\w-]+)\)", value)
            if inner is None:
                break
            value, seen = raw.get(inner.group(1), "").strip(), seen + 1
        if re.fullmatch(r"#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}", value):
            out[name] = value
    return out


def _colour(value: str, theme: dict[str, str], over: str | None) -> str | None:
    """One CSS colour value as a literal hex, or None if it cannot be resolved.

    `over` is the surface a translucent mix is composited onto; a
    `color-mix(..., transparent)` has no colour of its own until it is.
    """
    value = " ".join(value.split())

    # A background that paints nothing leaves the text on whatever ancestor it
    # lands on, which is what `over` already iterates. Resolving it to that
    # surface measures the rule where it actually renders instead of dropping
    # it into the unresolvable pile.
    if value in ("none", "transparent", "inherit", "initial", "unset"):
        return over

    literal = re.fullmatch(r"#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}", value)
    if literal:
        return value

    token = re.fullmatch(r"var\((--[\w-]+)\)", value)
    if token:
        return theme.get(token.group(1))

    mix = re.fullmatch(
        r"color-mix\(in srgb, var\((--[\w-]+)\) ([\d.]+)%, (transparent|var\(--[\w-]+\))\)",
        value,
    )
    if mix:
        accent = theme.get(mix.group(1))
        if accent is None:
            return None
        weight = float(mix.group(2)) / 100
        if mix.group(3) == "transparent":
            return None if over is None else _blend(accent, over, weight)
        base = _colour(mix.group(3), theme, over)
        return None if base is None else _blend(accent, base, weight)

    return None


def _pairs(css: str, theme: dict[str, str]) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """Every rule setting both a colour and a background, resolved.

    A translucent background is measured against whichever surface gives the
    WORST ratio: the rule does not say which ancestor it lands on, and the
    answer must hold wherever it does.
    """
    measured: list[tuple[str, str, str, str]] = []
    unresolved: list[str] = []

    for selector, body in _blocks(css):
        sel = " ".join(selector.split())
        if sel.endswith(":root") or ":root" in sel:
            continue
        bg_decl = re.search(r"background(?:-color)?\s*:\s*([^;]+);", body)
        fg_decl = re.search(r"(?<![-\w])color\s*:\s*([^;]+);", body)
        if not (bg_decl and fg_decl):
            continue
        fg = _colour(fg_decl.group(1), theme, None)
        if fg is None:
            continue

        worst: tuple[float, str] | None = None
        for surface in SURFACES:
            base = theme.get(surface)
            if base is None:
                continue
            bg = _colour(bg_decl.group(1), theme, base)
            if bg is None:
                continue
            ratio = contrast(fg, bg)
            if worst is None or ratio < worst[0]:
                worst = (ratio, bg)
        if worst is None:
            unresolved.append(" ".join(bg_decl.group(1).split()))
            continue
        measured.append((sel, fg, worst[1], " ".join(fg_decl.group(1).split())))

    return measured, unresolved


assert CSS.is_file(), f"{CSS} is not present; this test would check nothing"
CSS_TEXT = _strip_comments(CSS.read_text(encoding="utf-8"))
assert CSS_TEXT.strip(), "styles.css is empty; this test would check nothing"
DARK, LIGHT = _tokens(CSS_TEXT)
THEMES = {"dark": DARK, "light": LIGHT}

CASES = [
    (theme_name, sel, fg, bg, decl)
    for theme_name, theme in THEMES.items()
    for sel, fg, bg, decl in _pairs(CSS_TEXT, theme)[0]
]


@pytest.mark.parametrize(
    "theme,selector,fg,bg,declared",
    CASES,
    ids=[f"{t}:{s[:44]}" for t, s, _f, _b, _d in CASES],
)
def test_text_clears_aa_on_its_own_background(
    theme: str, selector: str, fg: str, bg: str, declared: str
) -> None:
    """Every rule that paints text on a background it also paints."""
    ratio = contrast(fg, bg)
    assert ratio >= AA, (
        f"{theme} theme, `{selector}`: {declared} resolves to {fg} on {bg}, "
        f"which is {ratio:.2f}:1 -- under the {AA}:1 WCAG AA floor for normal "
        f"text. These are set at 10-11.5px, so the large-text exemption does "
        f"not apply. Use the accent's `-ink` variant for text on a tint of "
        f"itself, or darken/lighten the accent for the theme that fails."
    )


@pytest.mark.parametrize("token", ACCENTS)
@pytest.mark.parametrize("theme", sorted(THEMES))
def test_every_accent_is_readable_as_plain_text(theme: str, token: str) -> None:
    """An accent used as bare text must clear AA on all three surfaces.

    The case the pair scan above cannot see: `.tag.wait { color: var(--warn) }`
    sets no background of its own, and which surface it lands on depends on the
    DOM. So every surface has to hold. This is the check that caught light-mode
    `--warn` at 4.37:1 against `--surface-2`.
    """
    palette = THEMES[theme]
    colour = palette.get(token)
    assert colour is not None, f"{token} is not defined for the {theme} theme"
    for surface in SURFACES:
        base = palette.get(surface)
        assert base is not None, f"{surface} is not defined for the {theme} theme"
        ratio = contrast(colour, base)
        assert ratio >= AA, (
            f"{theme} theme: {token} ({colour}) on {surface} ({base}) is "
            f"{ratio:.2f}:1, under the {AA}:1 AA floor. {token} is used as bare "
            f"text, so it has to hold on whichever surface it lands on."
        )


def test_unresolvable_backgrounds_are_declared() -> None:
    """A background this cannot measure must be named, never silently dropped."""
    for theme_name, theme in THEMES.items():
        _measured, unresolved = _pairs(CSS_TEXT, theme)
        for value in unresolved:
            assert any(known in value for known in UNRESOLVED_BACKGROUNDS), (
                f"{theme_name} theme: the background {value!r} carries text and "
                f"this test cannot compute a ratio for it. Add it to "
                f"UNRESOLVED_BACKGROUNDS with the reason it is safe, or give the "
                f"rule a background that can be measured -- leaving it here "
                f"would mean an unreadable rule passes by being invisible."
            )


def test_the_scan_found_the_rules_it_is_meant_to_guard() -> None:
    """Guards the parser, not the app.

    A regex that stopped matching would empty CASES and every parametrised
    case above would vanish from the run without failing anything. Collecting
    nothing looks exactly like passing, so the count and the specific chips
    that caused this test to exist are asserted by name.
    """
    assert len(CASES) >= 60, f"only {len(CASES)} colour pairs found across both themes"
    selectors = {sel for _t, sel, _f, _b, _d in CASES}
    for expected in (".roll.failed", ".roll.succeeded", ".q-chip.bad", ".scope.tenant"):
        assert expected in selectors, f"{expected} is no longer being measured"
    assert set(THEMES) == {"dark", "light"}, "a theme stopped being parsed out of styles.css"
