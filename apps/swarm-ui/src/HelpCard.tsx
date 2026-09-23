import { useCallback, useEffect, useId, useLayoutEffect, useReducer, useRef, useState, type CSSProperties, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { HELP, type TopicId } from './help'

/**
 * THE `?`, AND THE CARD BEHIND IT (docs/web-ui/ui-audit-and-build-prompt.md §B7.2).
 *
 * A 14px `?` in the faint colour, inline, AFTER the label it explains and
 * never after a value -- a glyph tucked against a number reads as a footnote
 * marker on the number, which is how "12 ?" becomes a figure nobody trusts.
 *
 * IT OPENS ON HOVER, ON FOCUS, AND ON CLICK, AND THE LAST TWO ARE NOT
 * OPTIONAL. Hover does not exist on a touch device and does not survive a
 * screenshot. An operator pasting a screen into an incident channel at 3am
 * publishes exactly what hover hides -- which is the failure mode the honesty
 * rules on these screens exist to prevent. So:
 *
 *   hover   opens after 120ms, closes when the pointer leaves
 *   focus   opens immediately, so the keyboard reaches everything the mouse does
 *   click   PINS it, so it survives the pointer leaving and can be read,
 *           selected and tabbed into. Escape and an outside click dismiss.
 *
 * WHY THIS FILE IS SPLIT IN THREE. `helpTransition` is the whole behaviour and
 * it is a pure function; `HelpCardView` renders one state and has none; the
 * hook joins them and owns the timer. That split is not tidiness -- it is what
 * makes "opens on focus, not only on hover" a test rather than a claim. A
 * component with the behaviour welded into it can only be checked by driving a
 * real browser, and this repository's offline gate cannot install one.
 *
 * STYLING IS INLINE, ON PURPOSE. styles.css belongs to another track this
 * pass (the same call `AgentDetail.tsx` already made for its attempt card). An
 * inline style adds no selector and therefore cannot restyle another screen.
 * Every value is an existing token, so it still flips with the theme.
 */

// ---------------------------------------------------------------------------
// The behaviour, as a pure function
// ---------------------------------------------------------------------------

export type HelpEvent =
  | { kind: 'pointer-enter' }
  /** The 120ms hover delay elapsed with the pointer still on the trigger. */
  | { kind: 'hover-settled' }
  | { kind: 'pointer-leave' }
  | { kind: 'focus' }
  | { kind: 'blur' }
  | { kind: 'click' }
  | { kind: 'escape' }
  | { kind: 'outside-click' }

export interface HelpState {
  /** The card is on screen. */
  open: boolean
  /** Click-pinned. Survives the pointer leaving and the trigger losing focus. */
  pinned: boolean
  /** A pointer is on the trigger or the card. */
  hovered: boolean
  /** The trigger holds focus, as far as this state machine is concerned. */
  focused: boolean
}

export const HELP_CLOSED: HelpState = {
  open: false,
  pinned: false,
  hovered: false,
  focused: false,
}

/** 120ms (§B7.2). Long enough that crossing the glyph does not flash a card. */
export const HOVER_DELAY_MS = 120

/**
 * Every transition, in one place, with no DOM and no timer.
 *
 * `open` is derived at the end of each branch rather than set ad hoc, because
 * the bug this shape prevents is a card that is open for a reason that has
 * since gone away -- pinned then unpinned while the pointer sat elsewhere.
 */
export function helpTransition(state: HelpState, event: HelpEvent): HelpState {
  switch (event.kind) {
    case 'pointer-enter':
      // Hovering does NOT open it yet. `hover-settled` does, once the delay
      // has passed and the pointer is still here.
      return { ...state, hovered: true }

    case 'hover-settled':
      // The pointer may have left while the timer ran. If it did, the timer
      // firing must not open a card under a pointer that is gone.
      return state.hovered ? { ...state, open: true } : state

    case 'pointer-leave':
      return { ...state, hovered: false, open: state.pinned || state.focused }

    case 'focus':
      return { ...state, focused: true, open: true }

    case 'blur':
      return { ...state, focused: false, open: state.pinned || state.hovered }

    case 'click':
      // A click on a pinned card closes it -- the second press of a toggle.
      return state.pinned
        ? { ...state, pinned: false, open: state.hovered || state.focused }
        : { ...state, pinned: true, open: true }

    case 'escape':
    case 'outside-click':
      // A FULL reset, focus included, although the button may still hold DOM
      // focus after Escape. Keeping `focused: true` here would let the next
      // pointer-leave re-derive `open` as true and spring the card back open
      // immediately after it was dismissed. The DOM keeps focus where it is;
      // the next deliberate focus or click reopens.
      return HELP_CLOSED
  }
}

/** `role="tooltip"` when hovered or focused, `role="dialog"` when pinned (§B7.2). */
export function helpRole(state: HelpState): 'dialog' | 'tooltip' {
  return state.pinned ? 'dialog' : 'tooltip'
}

/**
 * The handlers the trigger carries, built from a dispatch and nothing else.
 *
 * Exported so a test can assert what each one sends WITHOUT a browser. This is
 * the single wiring between an interaction and the state machine; if `onFocus`
 * or `onClick` ever stops being here, the card is hover-only again and
 * `tests/helpcard.test.ts` fails by name.
 */
export interface HelpTriggerProps {
  onFocus: () => void
  onBlur: () => void
  onClick: () => void
  onKeyDown: (e: { key: string }) => void
}

export function helpTriggerProps(dispatch: (e: HelpEvent) => void): HelpTriggerProps {
  return {
    onFocus: () => dispatch({ kind: 'focus' }),
    onBlur: () => dispatch({ kind: 'blur' }),
    onClick: () => dispatch({ kind: 'click' }),
    onKeyDown: (e) => {
      if (e.key === 'Escape') dispatch({ kind: 'escape' })
    },
  }
}

/** Hover lives on the WRAPPER so the card itself counts as hovered. */
export interface HelpHoverProps {
  onPointerEnter: () => void
  onPointerLeave: () => void
}

export function helpHoverProps(dispatch: (e: HelpEvent) => void): HelpHoverProps {
  return {
    onPointerEnter: () => dispatch({ kind: 'pointer-enter' }),
    onPointerLeave: () => dispatch({ kind: 'pointer-leave' }),
  }
}

// ---------------------------------------------------------------------------
// The hook: the state machine, the 120ms timer, and the outside click
// ---------------------------------------------------------------------------

export function useHelpDisclosure(delayMs: number = HOVER_DELAY_MS) {
  const [state, dispatch] = useReducer(helpTransition, HELP_CLOSED)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const clear = useCallback(() => {
    if (timer.current !== null) {
      clearTimeout(timer.current)
      timer.current = null
    }
  }, [])

  const send = useCallback(
    (e: HelpEvent) => {
      // Any event other than the pointer arriving cancels a pending open: a
      // click that lands 40ms into the delay must not be undone by the timer
      // firing 80ms later and re-opening what the click just closed.
      if (e.kind === 'pointer-enter') {
        clear()
        timer.current = setTimeout(() => {
          timer.current = null
          dispatch({ kind: 'hover-settled' })
        }, delayMs)
      } else {
        clear()
      }
      dispatch(e)
    },
    [clear, delayMs],
  )

  useEffect(() => clear, [clear])

  // Escape and outside-click, while it is pinned. Registered only when pinned
  // so a page of forty cards adds no listeners at rest.
  useEffect(() => {
    if (!state.pinned) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') dispatch({ kind: 'escape' })
    }
    const onDown = () => dispatch({ kind: 'outside-click' })
    document.addEventListener('keydown', onKey)
    // Capture, so a click inside the card can stop it before it arrives here
    // without depending on where in the tree the card was portalled to.
    document.addEventListener('pointerdown', onDown, true)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('pointerdown', onDown, true)
    }
  }, [state.pinned])

  return { state, send, trigger: helpTriggerProps(send), hover: helpHoverProps(send) }
}

// ---------------------------------------------------------------------------
// The view: one state in, markup out, no behaviour
// ---------------------------------------------------------------------------

const WRAP: CSSProperties = {
  position: 'relative',
  display: 'inline-block',
  verticalAlign: 'baseline',
  lineHeight: 0,
}

const GLYPH: CSSProperties = {
  // A real <button>: a <span> with a click handler is not reachable by Tab,
  // which would make "opens on focus" untrue for the people who need it most.
  appearance: 'none',
  background: 'transparent',
  border: '1px solid var(--line)',
  borderRadius: '999px',
  color: 'var(--text-faint)',
  cursor: 'help',
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  fontWeight: 600,
  fontSize: 'var(--t-meta)',
  // --lh-flush: the box is the 14px circle below.
  lineHeight: 'var(--lh-flush)',
  fontFamily: 'var(--mono)',
  height: '14px',
  width: '14px',
  marginLeft: '5px',
  padding: 0,
  verticalAlign: 'middle',
}

/**
 * THE CARD DOES NOT DECIDE WHICH WAY IT OPENS; `useEdgeSafePlacement` does.
 *
 * This carried `left: 0` and nothing else, so every card opened rightward from
 * its anchor whatever sat to the right of it. Measured 2026-09-24 by opening
 * all 82 of them: nine were unusable. Three on Runtimes opened 98, 84 and 11px
 * past the viewport; two on Workflows opened 278 and 309px past it, which is
 * not a clipped card but an invisible one. One on Pools was painted over by a
 * sibling, and two on Submit opened below the fold with one of those also
 * under a button.
 *
 * Anchors near the right edge need to open LEFTWARD and anchors near the
 * bottom need to open UPWARD. CSS alone cannot see that -- anchor positioning
 * would, and is not available here -- so the decision is measured once on open
 * and written as an inline offset. `left`/`right`/`top`/`bottom` are therefore
 * deliberately absent below: the hook supplies exactly one horizontal and one
 * vertical anchor, and a default here would fight it.
 */
const CARD: CSSProperties = {
  /* `position` and the offsets come from `useEdgeSafePlacement`, which portals
     this to <body> and places it in VIEWPORT coordinates. It has to leave the
     anchor's subtree: on Pools the card sat at z-index 200 and was still
     painted over by `.cap-families`, a `position: static; z-index: auto` div,
     because an ancestor of the anchor opens a stacking context that ranks
     below it. No z-index on a trapped element can win that -- the comparison
     never reaches the top level. Portalling is the only fix that generalises
     to the next panel someone wraps in a transform or a filter. */
  // ABOVE THE SIBLINGS, not just above the parent's children. At 40 a card on
  // Pools was painted over by a panel that establishes its own stacking
  // context, so the card's z-index was being compared inside a context the
  // panel had already won. 200 clears every in-page surface in this app; the
  // dock and the drawer are higher still and are meant to be.
  zIndex: 200,
  width: 'max-content',
  minWidth: '240px',
  maxWidth: '360px',
  background: 'var(--surface)',
  border: '1px solid var(--line)',
  borderRadius: 'var(--radius)',
  boxShadow: '0 8px 24px rgba(0,0,0,.28)',
  padding: 'var(--ctl-s3)',
  textAlign: 'left',
  lineHeight: 1.5,
  cursor: 'auto',
}

/**
 * Which way a card may open without leaving the viewport.
 *
 * Measured against the GLYPH rather than the card: the card is what we are
 * placing, so its own position is the thing in flux, while the anchor is
 * fixed. `useLayoutEffect` so the flip happens before paint -- in an effect
 * the card would be seen off-screen for a frame and then jump.
 *
 * Re-measured on scroll and resize because both move the anchor under a card
 * that is already open; a card correct when it opened is not correct after the
 * page moves beneath it.
 */
export function useEdgeSafePlacement(
  open: boolean,
): [RefObject<HTMLSpanElement>, CSSProperties] {
  const ref = useRef<HTMLSpanElement>(null)
  const [place, setPlace] = useState<CSSProperties>({ visibility: 'hidden' })

  useLayoutEffect(() => {
    if (!open) return
    const measure = () => {
      const node = ref.current
      if (!node) return
      const anchor = node.getBoundingClientRect()
      // The widest the card is allowed to be, from CARD's own maxWidth. Read as
      // a number rather than trusting the rendered width: on the first pass the
      // card may not have been laid out yet.
      const CARD_MAX = 360
      const MARGIN = 8
      const overflowsRight = anchor.left + CARD_MAX + MARGIN > window.innerWidth
      const overflowsLeft = anchor.right - CARD_MAX - MARGIN < 0
      // Both sides overflow only on a viewport narrower than the card, where
      // the honest answer is to pin it to the left margin and let maxWidth
      // (min(44ch, 86vw) in the sheet) do the rest.
      // VIEWPORT COORDINATES, because the card is portalled to <body> and is
      // `position: fixed` -- it has no positioned ancestor to be relative to.
      // CLAMPED ON BOTH BRANCHES, and the second one is why. The right-flip
      // read `anchor.right - CARD_MAX` with no upper bound, which is correct
      // only while the anchor is on screen. The rail becomes a horizontal
      // SCROLLER below 900px, so a glyph scrolled off to the right reports an
      // `anchor.right` of ~1049 in a 390px viewport and the card opened 299px
      // past the edge -- the flip meant to keep it in view was the thing that
      // threw it out. One clamp applied to whichever side wins.
      const wanted = overflowsRight && !overflowsLeft ? anchor.right - CARD_MAX : anchor.left
      const maxLeft = Math.max(MARGIN, window.innerWidth - CARD_MAX - MARGIN)
      const horizontal: CSSProperties = { left: Math.min(Math.max(MARGIN, wanted), maxLeft) }

      // Vertical: below unless it would not fit there and fits better above.
      //
      // MEASURED, NOT ESTIMATED. A constant here was wrong by 16px on Submit --
      // the card cleared a 220px guess and then hung below the fold anyway,
      // because these cards are as tall as their content and one of them has a
      // values list. On the first pass the card is not laid out yet, so the
      // estimate is the fallback and the real height replaces it the moment
      // there is one; `measure` re-runs on scroll and resize, so the second
      // pass is never far away.
      const card = ref.current?.querySelector<HTMLElement>('[role="tooltip"], [role="note"]')
      const cardH = card?.getBoundingClientRect().height || 220
      const roomBelow = window.innerHeight - anchor.bottom - 6
      // CLAMPED, so "below the fold" cannot happen at all: if it fits below it
      // goes below, if it fits better above it goes above, and if neither has
      // room it is pinned inside the viewport rather than hanging out of it.
      // The +16px overflow on Submit was a card that fit NEITHER way; flipping
      // it merely moved which edge it escaped from.
      const below = anchor.bottom + 6
      const above = anchor.top - 6 - cardH
      const top =
        cardH <= roomBelow ? below
        : above >= MARGIN ? above
        : Math.max(MARGIN, Math.min(below, window.innerHeight - cardH - MARGIN))

      setPlace({ position: 'fixed', ...horizontal, top })
    }
    measure()
    window.addEventListener('scroll', measure, true)
    window.addEventListener('resize', measure)
    return () => {
      window.removeEventListener('scroll', measure, true)
      window.removeEventListener('resize', measure)
    }
  }, [open])

  return [ref, place]
}

const CARD_TITLE: CSSProperties = {
  display: 'block',
  margin: '0 0 4px',
  fontWeight: 600,
  fontSize: 'var(--t-meta)',
  lineHeight: 'var(--lh-meta)',
  fontFamily: 'var(--mono)',
  letterSpacing: '.04em',
  textTransform: 'uppercase',
  color: 'var(--text-faint)',
}

const CARD_BODY: CSSProperties = {
  display: 'block',
  margin: 0,
  fontSize: 'var(--t-body)',
  lineHeight: 'var(--lh-body)',
  color: 'var(--text-dim)',
  // 52ch (§B7.2). A help card wider than a paragraph is a paragraph.
  maxWidth: '52ch',
}

const CARD_VALUES: CSSProperties = {
  display: 'flex',
  flexWrap: 'wrap',
  gap: '4px',
  margin: '8px 0 0',
  padding: 0,
  listStyle: 'none',
}

const CARD_VALUE: CSSProperties = {
  border: '1px solid var(--line)',
  borderRadius: 'var(--ctl-radius-sm)',
  padding: '1px 6px',
  fontWeight: 500,
  fontSize: 'var(--t-meta)',
  lineHeight: 'var(--lh-meta)',
  fontFamily: 'var(--mono)',
  color: 'var(--text-dim)',
  background: 'var(--surface-2)',
}

const CARD_LINK: CSSProperties = {
  display: 'inline-block',
  marginTop: '8px',
  fontSize: 'var(--t-micro)',
  lineHeight: 'var(--lh-micro)',
  color: 'var(--text-dim)',
}

const HIDDEN: CSSProperties = {
  position: 'absolute',
  width: '1px',
  height: '1px',
  overflow: 'hidden',
  clip: 'rect(0 0 0 0)',
  whiteSpace: 'nowrap',
}

export interface HelpCardViewProps {
  topic: TopicId
  state: HelpState
  /**
   * The id the EXPLAINED element points its `aria-describedby` at. The short
   * text is rendered into a node with this id whether the card is open or not,
   * so a screen reader gets the explanation from the label itself and never
   * has to find and open a popup to hear it.
   */
  descriptionId: string
  cardId: string
  trigger: HelpTriggerProps
  hover: HelpHoverProps
}

export function HelpCardView({
  topic,
  state,
  descriptionId,
  cardId,
  trigger,
  hover,
}: HelpCardViewProps) {
  const t = HELP[topic]
  const values = t.values?.() ?? []
  const [anchorRef, placement] = useEdgeSafePlacement(state.open)

  const cardNode = (
        <span id={cardId} role={helpRole(state)} style={{ ...CARD, ...placement }}>
          <strong style={CARD_TITLE}>{t.title}</strong>
          <span style={CARD_BODY}>{t.short}</span>
          {values.length > 0 && (
            <ul style={CARD_VALUES}>
              {values.map((v) => (
                <li key={v.term} style={CARD_VALUE} title={v.note}>
                  {v.term}
                </li>
              ))}
            </ul>
          )}
          <a href={`#${t.anchor}`} style={CARD_LINK}>
            Full explanation &rarr;
          </a>
        </span>
  )

  return (
    <span style={WRAP} {...hover} ref={anchorRef}>
      <button
        type="button"
        style={GLYPH}
        aria-label={`What ${t.title.charAt(0).toLowerCase()}${t.title.slice(1)} means`}
        aria-expanded={state.open}
        aria-controls={state.open ? cardId : undefined}
        {...trigger}
      >
        ?
      </button>

      {/* ALWAYS PRESENT, never drawn. `aria-describedby` on the label points
          here, so the explanation is available to assistive technology at the
          label without any interaction at all.

          `data-help-description` MARKS IT, and that attribute is not
          decoration. This node is in `document.body.textContent` whether the
          card is open or shut, so a test asking "what can a reader see with
          every card closed" reads the whole explanation back out of it and
          passes for the wrong reason -- the exact shape of failure the prose
          migration is most at risk of. `visibleText()` in
          `src/__tests__/honesty.prose.test.tsx` strips it by this attribute. */}
      <span id={descriptionId} data-help-description="" style={HIDDEN}>
        {t.short}
      </span>

      {/* PORTALLED ONLY WHERE THERE IS A DOCUMENT TO PORTAL INTO.
          `tests/run.mjs` renders this component to a STRING with no DOM, and
          `createPortal(…, document.body)` throws `document is not defined`
          there. Rendered inline in that case the markup is identical in every
          respect those tests assert -- they read the card's text, its role and
          its values, none of which the portal changes. What the portal changes
          is which stacking context the node lands in, and a string has none. */}
      {state.open &&
        (typeof document === 'undefined' ? (
          cardNode
        ) : (
          createPortal(
          cardNode,
          document.body,
          )
        ))}
    </span>
  )
}

// ---------------------------------------------------------------------------
// The component screens use
// ---------------------------------------------------------------------------

/**
 * `<HelpCard topic="absent-vs-zero" describedById={id} />`
 *
 * `describedById` is the id this card's short text is published at. The caller
 * puts it on the element being explained, as `aria-describedby`. It is a
 * parameter rather than something generated here because the association runs
 * from the LABEL to the description, and only the caller owns the label.
 * Callers with nothing to associate may leave it out; the description node is
 * then referenced by the trigger itself.
 */
export function HelpCard({
  topic,
  describedById,
}: {
  topic: TopicId
  describedById?: string
}) {
  const auto = useId()
  const { state, trigger, hover } = useHelpDisclosure()
  return (
    <HelpCardView
      topic={topic}
      state={state}
      descriptionId={describedById ?? `${auto}-desc`}
      cardId={`${auto}-card`}
      trigger={trigger}
      hover={hover}
    />
  )
}

// ---------------------------------------------------------------------------
// The footer a migrated panel carries in place of its legend
// ---------------------------------------------------------------------------

const LINKS: CSSProperties = {
  display: 'flex',
  flexWrap: 'wrap',
  alignItems: 'baseline',
  gap: '4px var(--ctl-s2)',
  margin: 'var(--ctl-s3) 0 0',
  paddingTop: 'var(--ctl-s3)',
  borderTop: '1px solid var(--line)',
  // Longhands, not a `font:` shorthand: jsdom does not expand a shorthand
  // carrying a custom property into longhands, so the DOM half of
  // typescale.test.ts would read nothing back from it. 1.45 is --lh-micro
  // rather than the 1.7 this was written with; the row gap above already
  // supplies the separation the looser line-height was doing by hand.
  fontSize: 'var(--t-micro)',
  lineHeight: 'var(--lh-micro)',
  fontFamily: 'var(--mono)',
  color: 'var(--text-faint)',
}

const LINK: CSSProperties = { color: 'var(--text-dim)' }

/**
 * A `<section className="section legend">` block, after its prose has moved.
 *
 * WHY A LIST OF LINKS RATHER THAN NOTHING. The legends this replaces were the
 * only index of what a screen's marks mean, and deleting one takes the index
 * out along with the essay. So the titles stay on the surface -- two to six
 * words each -- and the paragraphs live at `#help/<id>`.
 *
 * NOTHING MEASURED MAY DEPEND ON IT. A screen that needs this footer followed
 * before an absent figure can be told from a zero has moved a fact rather than
 * an explanation, and `src/__tests__/honesty.prose.test.tsx` renders those
 * screens with every card closed and asserts otherwise.
 *
 * `AgentDetail.tsx` grew this shape first, as its own `AttemptLegend`. That
 * file belongs to the exemplar lane, so this is the same markup lifted rather
 * than that component reused.
 *
 * Inline styles for the reason the rest of this file gives: `styles.css`
 * belongs to another track this pass, and an inline style adds no selector and
 * therefore cannot restyle another screen. Every value is an existing token.
 */
export function HelpLinks({
  topics,
  label = 'Reading this screen:',
}: {
  topics: readonly TopicId[]
  label?: string
}) {
  return (
    <p style={LINKS}>
      <span>{label}</span>
      {topics.map((id) => (
        <a key={id} href={`#${HELP[id].anchor}`} style={LINK}>
          {HELP[id].title}
        </a>
      ))}
    </p>
  )
}
