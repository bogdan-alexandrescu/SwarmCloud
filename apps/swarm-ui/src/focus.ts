/**
 * WHAT "KEYBOARD-REACHABLE" MEANS IN THIS APP, WRITTEN ONCE.
 *
 * WHY THIS FILE EXISTS. 588 interactive elements were exercised by click and
 * never by Tab, and the sweep that establishes otherwise
 * (`src/__tests__/keyboard.test.tsx`) has to agree with the components about
 * which elements are tab stops -- otherwise the sweep measures its own
 * definition rather than the app's. Every restatement of a rule in this
 * repository has since drifted, so the drawer's focus trap, the help card's
 * tab bridge and the sweep all import `tabStops` from here.
 *
 * THAT MAKES THE DEFINITION ITSELF UNTESTED BY THE SWEEP, which is the honest
 * cost of sharing it: a sweep that imports the thing it measures cannot notice
 * the thing breaking. `keyboard.test.tsx` therefore drives `tabStops` over a
 * hand-built fixture with a known answer, first, before it renders anything --
 * so a broken definition fails by name rather than quietly emptying every
 * later count.
 *
 * NO REACT IN THE IMPORT GRAPH, for the reason `checks.ts` gives: these are
 * DOM predicates, and a component that imports them must not drag a test
 * runner's worth of React into a module that only reads attributes.
 */

/**
 * The elements a browser puts in the sequential focus order by default, plus
 * anything the markup opted in with `tabindex`.
 *
 * `[tabindex]` WITHOUT A VALUE FILTER, deliberately. `tabindex="-1"` matches
 * this selector and is removed by `isTabStop` below, because the two questions
 * are different: "is this element focusable at all" (a trap target, a
 * programmatic `.focus()` destination) and "does Tab stop here". Collapsing
 * them into one selector is how a `tabindex="-1"` panel stops being a legal
 * place to send focus when a modal opens.
 *
 * `audio`/`video` carry `controls` because a media element without it has no
 * focusable UI. `iframe`, `object` and `embed` are focusable containers in
 * every engine and are listed although this app ships none today -- the sweep
 * has to see one the day somebody adds it, and a selector that names only what
 * exists today is a selector that goes stale silently.
 */
export const FOCUSABLE_SELECTOR = [
  'a[href]',
  'area[href]',
  'button',
  'input',
  'select',
  'textarea',
  'summary',
  'iframe',
  'object',
  'embed',
  'audio[controls]',
  'video[controls]',
  '[tabindex]',
  '[contenteditable=""]',
  '[contenteditable="true"]',
].join(',')

/** The `tabindex` an element declares, or null when it declares none. */
export function declaredTabIndex(el: Element): number | null {
  const raw = el.getAttribute('tabindex')
  if (raw === null) return null
  const n = Number.parseInt(raw.trim(), 10)
  return Number.isFinite(n) ? n : null
}

/**
 * Hidden by the sheet, rather than by an attribute.
 *
 * THIS IS NOT A CONVENIENCE. `styles.css` carries
 * `.ctl-drawer > .drawer > .drawer-close { display: none }` -- the inner close
 * button `AgentDetail.tsx` draws, flattened away when the drawer is nested
 * inside the inspector. A browser takes a `display: none` element out of the
 * focus order; a probe that only reads attributes does not, and would report a
 * button nobody can see as a tab stop on every route that opens an agent. So
 * the computed value is read, from the element's own view.
 *
 * `visibility: hidden` is the same rule and `opacity: 0` is NOT: an element at
 * zero opacity is still focusable in every engine, and if this app ever draws
 * one it is a real finding rather than something to filter away.
 */
export function hiddenByStyle(el: Element): boolean {
  const view = el.ownerDocument?.defaultView
  if (!view) return false
  for (let node: Element | null = el; node !== null; node = node.parentElement) {
    const s = view.getComputedStyle(node)
    if (s.display === 'none') return true
    // `visibility` inherits, so an ancestor's `hidden` reaches the element --
    // but a descendant may set it back to `visible`, which is why only the
    // element's own computed value decides and the walk above it looks at
    // `display` alone.
    if (node === el && s.visibility === 'hidden') return true
  }
  return false
}

/**
 * Focusable at all: a legal destination for `.focus()`.
 *
 * `inert` is checked although nothing in this app sets it yet: an inert
 * subtree is exactly the mechanism a future modal would use, and a focus
 * utility that did not know about it would hand focus to something the browser
 * refuses to focus, leaving `document.activeElement` on `<body>` with nothing
 * saying why.
 */
export function isFocusable(el: Element): boolean {
  const view = el.ownerDocument?.defaultView
  if (!view || !(el instanceof view.HTMLElement)) return false
  if (el.hasAttribute('disabled')) return false
  if (el.closest('[inert]') !== null) return false
  if (el.hasAttribute('hidden')) return false
  if (hiddenByStyle(el)) return false
  return true
}

/**
 * Does Tab stop here.
 *
 * `aria-hidden` IS NOT FILTERED OUT, and that is the point. A focusable
 * element inside an `aria-hidden="true"` subtree is still a tab stop in every
 * engine -- the attribute hides it from assistive technology and from nothing
 * else -- so a screen-reader user lands on a control their reader cannot name.
 * That is a finding for the sweep to report, not a case for this function to
 * swallow.
 */
export function isTabStop(el: Element): boolean {
  if (!isFocusable(el)) return false
  const t = declaredTabIndex(el)
  return t === null || t >= 0
}

/**
 * Every tab stop inside `root`, in the order Tab visits them.
 *
 * DOCUMENT ORDER IS THE TAB ORDER **because this app declares no positive
 * `tabindex`**, and that is an assertion rather than a hope: a positive
 * tabindex hoists an element to the front of the document's sequence
 * regardless of where it sits, and `keyboard.test.tsx` fails on any element
 * that declares one. If that assertion is ever relaxed, this function has to
 * grow the two-bucket sort the spec describes -- so the constraint is recorded
 * here, next to the code that depends on it.
 */
export function tabStops(root: ParentNode): HTMLElement[] {
  const out: HTMLElement[] = []
  for (const el of root.querySelectorAll(FOCUSABLE_SELECTOR)) {
    if (isTabStop(el)) out.push(el as HTMLElement)
  }
  return out
}

/**
 * Keep Tab inside `panel`, and answer whether it was intercepted.
 *
 * ONLY FOR A PANEL THAT COVERS WHAT IS BEHIND IT. The caller decides -- see
 * `isOverlay` -- because the same markup is a modal overlay below 1100px and a
 * side-by-side grid column above it, and trapping focus in a column that sits
 * BESIDE the list would be a worse bug than the one this fixes: the list is
 * right there, visible, and Tab would refuse to reach it.
 *
 * THE RETURN VALUE IS WHAT MAKES "NO FOCUS TRAP ANYWHERE ELSE" MEASURABLE. The
 * only way to trap focus in this app is to call `preventDefault` on a Tab
 * keydown, so the sweep presses Tab on every tab stop it finds and holds the
 * set of elements that swallow it to a named list.
 */
export function trapTab(
  e: { key: string; shiftKey: boolean; preventDefault: () => void },
  panel: HTMLElement | null,
): boolean {
  if (e.key !== 'Tab' || panel === null) return false
  const stops = tabStops(panel)
  if (stops.length === 0) return false
  const first = stops[0]!
  const last = stops[stops.length - 1]!
  const active = panel.ownerDocument.activeElement
  // The panel itself counts as "before the first stop": it is given
  // `tabindex="-1"` and focused when the overlay opens, so Shift+Tab from
  // there has to wrap to the end rather than escape to the page behind.
  if (e.shiftKey && (active === first || active === panel)) {
    e.preventDefault()
    last.focus()
    return true
  }
  if (!e.shiftKey && active === last) {
    e.preventDefault()
    first.focus()
    return true
  }
  return false
}

/**
 * Is this panel drawn OVER the page rather than beside it.
 *
 * READ OFF THE SHEET, NOT FROM A BREAKPOINT REPEATED IN TYPESCRIPT. The drawer
 * is `position: fixed` by `.drawer` and `position: sticky` by the
 * `@media (min-width: 1100px)` block in `styles.css`. Writing `1100` here
 * would be a second definition of the breakpoint, and this file's header says
 * what happens to those. Reading the computed `position` asks the sheet that
 * actually shipped which layout it chose.
 *
 * ANYTHING THAT IS NOT `sticky` IS TREATED AS AN OVERLAY, including the
 * `static` a document with no stylesheet reports. That direction is
 * deliberate: defaulting to "modal" gives a keyboard user a boundary they can
 * always leave through the close button or Escape, while defaulting to
 * "beside" would silently drop the trap the moment a stylesheet failed to
 * load.
 */
export function isOverlay(panel: HTMLElement): boolean {
  const view = panel.ownerDocument.defaultView
  if (!view) return true
  return view.getComputedStyle(panel).position !== 'sticky'
}

/**
 * A pane size, nudged one step by an arrow key. Pure, so it is tested without
 * a DOM alongside `clampPane` in `panes.ts`.
 *
 * WHY A RESIZER NEEDS THIS AT ALL. The inspector's grip and the dock's grip
 * were `<div role="separator">` carrying four pointer handlers and nothing
 * else: reachable by mouse, unreachable by keyboard, which is the first thing
 * the keyboard sweep was asked to look for and the first thing it found. The
 * WAI-ARIA window-splitter pattern makes the separator a tab stop and moves it
 * with the arrow keys, which is what these now do.
 *
 * STEP IS 16px, NOT 1px. The inspector's range is 400-720px: at one pixel a
 * step, crossing it is 320 keystrokes, which is a control that technically
 * responds and practically does not. 16px crosses the inspector in 20 presses
 * and the dock's 120-640px in 33, and it is also this app's `--ctl-s4` rhythm,
 * so a keyboard drag lands on the same sizes a pointer drag does.
 *
 * `Home` and `End` are the whole range, because a viewer who wants the pane
 * out of the way wants it out of the way now.
 */
export const PANE_STEP_PX = 16

export function nudgePane(
  current: number,
  key: string,
  bounds: { min: number; max: number },
  /** Which arrow grows the pane. The inspector is anchored right, so LEFT widens it. */
  grow: 'ArrowLeft' | 'ArrowUp',
): number | null {
  const shrink = grow === 'ArrowLeft' ? 'ArrowRight' : 'ArrowDown'
  if (key === 'Home') return bounds.min
  if (key === 'End') return bounds.max
  if (key === grow) return Math.min(bounds.max, current + PANE_STEP_PX)
  if (key === shrink) return Math.max(bounds.min, current - PANE_STEP_PX)
  return null
}
