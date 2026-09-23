// THE KEYBOARD PROBE. What `spaceprobe.ts` is to spacing, this is to focus.
//
// WHY IT IS A SEPARATE FILE FROM THE SWEEP. Same reason as `spaceprobe.ts`:
// the physics and the thresholds live where they can be read and argued with,
// and the sweep beside it is then a list of routes and four assertions.
//
// WHAT IT CAN SEE, AND WHAT IT CANNOT -- READ THIS BEFORE TRUSTING A ZERO.
//
//   IT CANNOT SEE A REACT HANDLER. A rendered DOM carries no `onClick`, so
//   "this div does something when you click it" is not a question this file
//   can ask directly. It asks the two questions that ARE in the DOM and that
//   catch the same defect: an element the SHEET gives an interactive cursor to
//   and nothing makes focusable, and an element whose ARIA role promises a
//   control the keyboard cannot reach. Every mouse-only control this pass
//   actually found -- both pane resizers -- was found by the first of those.
//
//   IT CANNOT SEE LAYOUT. jsdom has no layout engine, so "tab order follows
//   VISUAL order" is not directly measurable here and this file does not
//   claim it. What it measures instead is the two ways visual order and tab
//   order come apart in this app: a POSITIVE `tabindex`, which hoists an
//   element out of document order wherever it sits, and a PORTAL, which moves
//   a node to the end of `<body>` while leaving its trigger mid-page. The
//   portalled help card is what prompted this whole lane and is exactly the
//   second case. Overlap, reading order within a row, and a flex `order`
//   property are NOT covered, and a browser-driven pass is the only thing that
//   would cover them.
//
//   IT CANNOT PRESS TAB. jsdom implements no sequential focus navigation:
//   dispatching a Tab keydown moves nothing. That is not the gap it sounds
//   like, because in this app the only way to CHANGE where Tab goes is a
//   handler calling `preventDefault` on that keydown -- so the sweep presses
//   Tab on every stop it finds and holds the set of elements that swallow it
//   to a named list. A trap that is not in that list is a trap that was not
//   declared.

import { declaredTabIndex, isFocusable, tabStops } from '../focus'

export type Kind =
  /** A `tabindex` above zero. It reorders the whole document's tab sequence. */
  | 'positive-tabindex'
  /** The sheet gives it a hand cursor; nothing gives it a keyboard path. */
  | 'mouse-only'
  /** An ARIA role that promises a control, on something Tab cannot reach. */
  | 'role-without-focus'
  /** A tab stop outside the app root that declares no way back. */
  | 'stranded'
  /** A tab stop inside an `aria-hidden` subtree: reachable, and unnameable. */
  | 'aria-hidden-stop'
  /** The sheet removes an outline and restores nothing in its place. */
  | 'ring-removed'

export interface Finding {
  kind: Kind
  where: string
  detail: string
}

export interface Report {
  findings: Finding[]
  /**
   * Every tab stop in the document, in order, HANDED BACK rather than counted
   * and thrown away. The sweep presses Tab on each of them afterwards, and
   * recomputing the list would mean a second `getComputedStyle` pass over
   * every element in the document -- which is the slowest thing in this suite
   * and the reason `spacing.test.tsx` carries three comments about it.
   */
  stopElements: HTMLElement[]
  /** Tab stops counted. The number the floor assertion is about. */
  stops: number
  /** Every element whose computed style was read. The denominator. */
  elements: number
  /** Tab stops matched by some `:focus-visible` rule in the shipped sheet. */
  ringed: number
  /** Selectors jsdom would not evaluate. Never silently skipped. */
  unsupported: string[]
}

/** A readable name for an element, stable across runs. Ids here are generated. */
export function signature(el: Element): string {
  const cls = [...el.classList].sort().join('.')
  const role = el.getAttribute('role')
  return (
    el.tagName.toLowerCase() +
    (cls === '' ? '' : `.${cls}`) +
    (role === null ? '' : `[role=${role}]`)
  )
}

/**
 * Cursors that say "this is a control".
 *
 * `help` IS NOT HERE and that is deliberate: it is what the `?` glyph wears,
 * and the glyph is a real `<button>`. `not-allowed` is not here either -- a
 * disabled control is correctly out of the tab order.
 */
const POINTY = new Set(['pointer', 'col-resize', 'row-resize'])

/**
 * Roles that promise a control the keyboard must reach.
 *
 * `separator` is ABSENT AND THEN ADDED BACK CONDITIONALLY. A plain
 * `role="separator"` is a static divider and is correctly not focusable; a
 * separator carrying `aria-valuenow` is a window splitter, which the ARIA
 * spec defines as focusable and which this app has two of. Testing for the
 * value attribute rather than the role is what tells the two apart, and it is
 * the exact mutation guard for the fix: delete `tabIndex={0}` from either grip
 * and this fires by name.
 */
const CONTROL_ROLES = new Set([
  'button',
  'link',
  'checkbox',
  'radio',
  'switch',
  'tab',
  'menuitem',
  'menuitemcheckbox',
  'menuitemradio',
  'option',
  'slider',
  'spinbutton',
  'textbox',
  'combobox',
  'treeitem',
])

function promisesAControl(el: Element): boolean {
  const role = el.getAttribute('role')
  if (role === null) return false
  if (CONTROL_ROLES.has(role)) return true
  return role === 'separator' && el.hasAttribute('aria-valuenow')
}

/**
 * Something inside this element is a tab stop, so the element itself does not
 * have to be one.
 *
 * This is what keeps a `<label>` wrapping a radio, and a container that draws
 * a hand cursor over the button inside it, from reading as a mouse-only
 * control. `.sbf-runner` and `.dsp-option` on the two submit forms are both
 * exactly that shape.
 */
function wrapsAControl(el: Element): boolean {
  return tabStops(el).length > 0
}

/**
 * A control that is switched off, and therefore correctly out of the tab order.
 *
 * THIS IS THE ONE PLACE THE SELECTOR-MATCHING APPROACH NEEDS HELP, and the
 * first run of this sweep is what showed it: ten findings, every one of them a
 * `<button disabled>` -- the submit button before a runner is chosen, the save
 * beside an unedited pool limit, the danger buttons on an account card.
 *
 * They matched `:where(.app button)`, which does declare `cursor: pointer`.
 * What a selector match CANNOT see is the rule two lines below it,
 * `:where(.app button:disabled) { cursor: not-allowed }`, which takes the hand
 * cursor away again for exactly this case. The sheet is already saying "you
 * cannot press this"; the probe was reading only the first half of the
 * sentence.
 *
 * A disabled control is not a mouse-only control either way: the mouse cannot
 * use it any more than the keyboard can, so it is not the defect this check is
 * looking for. `aria-disabled` counts as well, because a control that is
 * disabled in the accessibility tree and focusable in the DOM is a different
 * finding from this one and would be reported here for the wrong reason.
 */
function isDisabled(el: Element): boolean {
  return el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true'
}

export interface ProbeOptions {
  /**
   * The element React rendered into. Anything focusable in `<body>` but
   * outside it has been portalled, which is what the `stranded` check is about.
   */
  appRoot: Element
  /** Selectors from the shipped sheet that draw a focus ring, already stripped. */
  ringSelectors: readonly string[]
  /** Selectors from the shipped sheet that give an element a control's cursor. */
  pointerSelectors: readonly string[]
}

/**
 * One rendered screen, read.
 *
 * The root is `document.body` rather than the render container on purpose: a
 * portalled card is a sibling of the container, and the defect this lane was
 * called for lives precisely in the gap between those two.
 *
 * THE HAND CURSOR IS FOUND BY SELECTOR, NOT BY COMPUTED STYLE, and the reason
 * is `cursor`'s inheritance. Every `<span>` inside a clickable row computes
 * `cursor: pointer`, so reading the computed value would report forty findings
 * a screen for one rule, and the obvious repair -- "only flag it where the
 * value differs from the parent's" -- is a second guess about which element
 * the sheet was talking to. Matching the sheet's own selector IS that
 * question, answered exactly. It is also what lets this run at all: the
 * computed-style version reads every element on every route, which is the walk
 * `spacing.test.tsx` measured at two minutes.
 */
export function probeKeyboard(doc: Document, opts: ProbeOptions): Report {
  // `isFocusable` reads computed styles off each element's own view, so a
  // document with none would answer "focusable" for things a browser hides.
  if (doc.defaultView === null) {
    throw new Error('the probe needs a window to compute styles with')
  }

  const findings: Finding[] = []
  const unsupported: string[] = []
  const said = new Set<string>()
  const say = (f: Finding): void => {
    const key = `${f.kind}|${f.where}|${f.detail}`
    if (said.has(key)) return
    said.add(key)
    findings.push(f)
  }

  for (const sel of opts.pointerSelectors) {
    // Initialised rather than assigned only in the `try`, so the compiler does
    // not have to reason about whether the `catch` escaped.
    let matched: Element[] = []
    try {
      matched = [...doc.body.querySelectorAll(sel)]
    } catch {
      if (!unsupported.includes(sel)) unsupported.push(sel)
      continue
    }
    for (const el of matched) {
      // A `<label>` reaches its control by being clicked, and a `<span>` inside
      // a button is reached by reaching the button. Only an element with no
      // control of its own, none inside it and none above it is stranded.
      if (el.tagName === 'LABEL') continue
      // See `isDisabled`: the sheet's own `:disabled` rule takes this cursor
      // back, and a selector match cannot see a later rule override it.
      if (isDisabled(el)) continue
      if (isFocusable(el)) continue
      const parent = el.parentElement
      if (parent !== null && parent.closest('button, a[href], [tabindex]') !== null) continue
      if (wrapsAControl(el)) continue
      say({
        kind: 'mouse-only',
        where: signature(el),
        detail: `\`${sel}\` gives it a control's cursor and nothing makes it focusable`,
      })
    }
  }

  for (const el of doc.body.querySelectorAll('[role]')) {
    if (!promisesAControl(el)) continue
    // A switched-off control is meant to be out of the tab order.
    if (isDisabled(el)) continue
    if (isFocusable(el)) continue
    say({
      kind: 'role-without-focus',
      where: signature(el),
      detail: `role="${el.getAttribute('role') ?? ''}" on an element Tab cannot reach`,
    })
  }

  const elements = doc.body.querySelectorAll('*').length
  // THE WHOLE BODY, not the render container: a portalled card is a SIBLING of
  // the container, and the gap between those two is the defect this lane was
  // called for.
  const stops = tabStops(doc.body)
  let ringed = 0

  for (const el of stops) {
    const t = declaredTabIndex(el)
    if (t !== null && t > 0) {
      say({
        kind: 'positive-tabindex',
        where: signature(el),
        detail: `tabindex="${t}" hoists this out of document order for the whole document`,
      })
    }

    if (el.closest('[aria-hidden="true"]') !== null) {
      say({
        kind: 'aria-hidden-stop',
        where: signature(el),
        detail: 'Tab stops here and a screen reader has nothing to announce',
      })
    }

    if (!opts.appRoot.contains(el)) {
      const home = el.closest('[data-focus-return]')
      const back = home?.getAttribute('data-focus-return') ?? null
      if (home === null) {
        say({
          kind: 'stranded',
          where: signature(el),
          detail:
            'portalled out of the app root with no `data-focus-return`: Tab reaches it ' +
            'after every other control on the page',
        })
      } else if (back === null || doc.getElementById(back) === null) {
        say({
          kind: 'stranded',
          where: signature(el),
          detail: `data-focus-return="${back ?? ''}" names no element in the document`,
        })
      }
    }

    for (const sel of opts.ringSelectors) {
      try {
        if (el.matches(sel)) {
          ringed += 1
          break
        }
      } catch {
        // A selector this engine will not parse is recorded, never skipped: a
        // silent skip turns "nothing draws a ring on this" into a finding that
        // is about jsdom rather than about the app.
        if (!unsupported.includes(sel)) unsupported.push(sel)
      }
    }
  }

  return { findings, stopElements: stops, stops: stops.length, elements, ringed, unsupported }
}

// ---------------------------------------------------------------------------
// Reading the shipped sheet
// ---------------------------------------------------------------------------

export interface Rule {
  selector: string
  body: string
}

/**
 * Every style rule in a sheet, at-rules descended into rather than skipped.
 *
 * WHY NOT A REGEX. `styles.css` is 7,600 lines with nested `@media` blocks and
 * `:where(a, b, c)` selectors carrying commas inside parentheses. Both break
 * the obvious `/([^{]+)\{([^}]*)\}/g`, and they break it by producing FEWER
 * rules rather than an error -- which would quietly shrink the ring coverage
 * this file reports and make the sheet look better than it is.
 *
 * A rule inside `@media` counts. It draws a ring at some width, and a probe
 * that ignored media blocks would report a control as unringed because its
 * only rule happens to live in the phone block.
 */
export function rules(css: string): Rule[] {
  const s = css.replace(/\/\*[\s\S]*?\*\//g, '')
  const out: Rule[] = []
  let i = 0
  let selStart = 0
  while (i < s.length) {
    const ch = s[i]
    if (ch === '{') {
      const sel = s.slice(selStart, i).trim()
      if (sel.startsWith('@')) {
        // Descend: the rules inside are rules, and the at-rule's own `}` is
        // handled by the `}` branch below, which just resets the selector.
        i += 1
        selStart = i
        continue
      }
      let depth = 1
      let j = i + 1
      for (; j < s.length && depth > 0; j += 1) {
        if (s[j] === '{') depth += 1
        else if (s[j] === '}') depth -= 1
      }
      out.push({ selector: sel, body: s.slice(i + 1, j - 1) })
      i = j
      selStart = i
      continue
    }
    if (ch === '}' || ch === ';') {
      i += 1
      selStart = i
      continue
    }
    i += 1
  }
  return out
}

/** `:focus-visible` and friends removed, so the remainder can be matched. */
function withoutFocus(selector: string): string {
  return selector.replace(/:focus-visible|:focus-within|:focus/g, '').trim()
}

/**
 * The selectors this sheet draws a focus ring with, stripped of the pseudo so
 * an unfocused element can be matched against them.
 *
 * STRIPPED RATHER THAN MATCHED DIRECTLY, because `:focus-visible` is a user
 * interaction state: no element in a test document is in it, and asking
 * `el.matches(':focus-visible')` would answer false for every element and
 * report a sheet with no focus rules at all. The question this file actually
 * wants answered is "is there a rule that WOULD draw a ring on this element",
 * and that is the selector without the state.
 *
 * A comma-separated selector list is split, because `matches()` on the whole
 * list would be true for an element matching any branch -- which is the right
 * answer, but the split gives a far better failure message.
 */
export function ringSelectors(css: string): string[] {
  const out: string[] = []
  for (const r of rules(css)) {
    if (!/:focus(-visible|-within)?\b/.test(r.selector)) continue
    if (!/\boutline\s*:/.test(r.body) && !/\bbox-shadow\s*:/.test(r.body)) continue
    for (const branch of splitSelectorList(r.selector)) {
      if (!/:focus(-visible|-within)?\b/.test(branch)) continue
      const stripped = withoutFocus(branch)
      if (stripped !== '' && !out.includes(stripped)) out.push(stripped)
    }
  }
  return out
}

/** Split on top-level commas only: `:where(a, b)` is one branch, not two. */
export function splitSelectorList(selector: string): string[] {
  const out: string[] = []
  let depth = 0
  let start = 0
  for (let i = 0; i < selector.length; i += 1) {
    const c = selector[i]
    if (c === '(' || c === '[') depth += 1
    else if (c === ')' || c === ']') depth -= 1
    else if (c === ',' && depth === 0) {
      out.push(selector.slice(start, i).trim())
      start = i + 1
    }
  }
  out.push(selector.slice(start).trim())
  return out.filter((s) => s !== '')
}

/**
 * The selectors this sheet gives a control's cursor to.
 *
 * WHY THIS IS THE RIGHT QUESTION TO ASK OF A DOM WITH NO HANDLERS IN IT. A
 * rendered tree carries no `onClick`, so "is this clickable" cannot be read
 * off it. But a control that responds to a click and does not say so with the
 * cursor is a control nobody discovers, and this sheet is disciplined about
 * it: all 31 rules that declare a hand cursor are on something the author
 * meant to be pressed. So the sheet's own `cursor` declarations are the
 * closest thing to a list of this app's controls that exists in a file, and
 * checking each of them against "can the keyboard reach it" is the check.
 *
 * INTERACTION-STATE SELECTORS ARE SKIPPED. `:hover`, `:active` and `:focus`
 * cannot match an element at rest, so matching against them would find
 * nothing; and a cursor that appears only on hover is not the affordance this
 * check is about. `:disabled` is skipped for the opposite reason -- a disabled
 * control is correctly out of the tab order, and `cursor: not-allowed` is
 * already excluded by the value filter.
 */
export function pointerSelectors(css: string): string[] {
  const out: string[] = []
  for (const r of rules(css)) {
    const m = /\bcursor\s*:\s*([a-z-]+)/.exec(r.body)
    if (m === null || !POINTY.has(m[1] ?? '')) continue
    for (const branch of splitSelectorList(r.selector)) {
      if (/:hover|:active|:focus|::/.test(branch)) continue
      if (!out.includes(branch)) out.push(branch)
    }
  }
  return out
}

/**
 * Rules that take a focus ring away without putting one back.
 *
 * THIS IS THE ASSERTION THAT MATTERS MOST IN THIS FILE, because it is the only
 * one here that can catch a defect nobody has written yet. Every engine draws
 * its own ring on a focused control, so a control with no rule in this sheet
 * is still visibly focused; the way a focus indicator actually disappears from
 * an app is `outline: none` written to tidy up a button's appearance, on a
 * selector with no `:focus-visible` beside it. There are none today. This is
 * what makes that stay true.
 *
 * A rule that suppresses the outline ONLY in the focus state -- `:focus {
 * outline: none }` next to `:focus-visible { outline: 2px }`, which is the
 * standard way to hide the ring from a mouse click -- is not a finding, and is
 * why the selector is checked as well as the declaration.
 */
export function outlineKillers(css: string): Finding[] {
  const out: Finding[] = []
  const ringed = new Set(ringSelectors(css))
  for (const r of rules(css)) {
    if (!/\boutline\s*:\s*(none|0)\b/.test(r.body)) continue
    for (const branch of splitSelectorList(r.selector)) {
      const stripped = withoutFocus(branch)
      // `:focus { outline: none }` paired with a `:focus-visible` rule that
      // reaches the same elements is the documented way to show the ring to a
      // keyboard and hide it from a click.
      if (/:focus/.test(branch) && ringed.has(stripped)) continue
      out.push({
        kind: 'ring-removed',
        where: branch,
        detail: 'removes the outline; no `:focus-visible` rule in this sheet puts one back',
      })
    }
  }
  return out
}
