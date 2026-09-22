import { useCallback, useEffect, useId, useReducer, useRef, type CSSProperties } from 'react'
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

const CARD: CSSProperties = {
  position: 'absolute',
  zIndex: 40,
  top: 'calc(100% + 6px)',
  left: 0,
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

  return (
    <span style={WRAP} {...hover}>
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
          label without any interaction at all. */}
      <span id={descriptionId} style={HIDDEN}>
        {t.short}
      </span>

      {state.open && (
        <span id={cardId} role={helpRole(state)} style={CARD}>
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
      )}
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
