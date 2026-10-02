/**
 * TOOLTIP, MENU, DIALOG AND THE TYPED CONFIRM (components.html A,
 * "Tooltips, hover cards, menus" and "Dialogs, including the typed confirm").
 *
 * TOOLTIP: one sentence, on hover AND focus, after 120ms (HelpCard's delay),
 * gone on Escape. It is never the only place a fact lives: the 236 native
 * `title=` tooltips it replaces were invisible to a keyboard and a phone.
 *
 * MENU: a DISCLOSURE of actions -- a button and a list of buttons -- not
 * `role="menu"`, which promises arrow keys and roving focus this does not
 * have (the reason Spine.tsx's flyout and Submit's add menu give). A disabled
 * item says why on its own line.
 *
 * DIALOG: modal. Focus moves in on open, Tab wraps inside it, Escape and the
 * scrim close it, and focus returns to what opened it. The destructive action
 * sits on the right and says its verb.
 *
 * TYPED CONFIRM: the dialog, plus a field that unlocks the destructive button
 * ONLY on an exact match of the name it asks for -- no trimming, no case
 * folding, because "close enough" is what the confirm exists to refuse.
 */
import { Fragment, useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Button } from './Button'

export const TOOLTIP_DELAY_MS = 120

export function Tooltip({ tip, children }: { tip: string; children: ReactNode }) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const show = (now: boolean) => {
    if (timer.current !== undefined) clearTimeout(timer.current)
    if (now) setOpen(true)
    else timer.current = setTimeout(() => setOpen(true), TOOLTIP_DELAY_MS)
  }
  const hide = () => {
    if (timer.current !== undefined) clearTimeout(timer.current)
    setOpen(false)
  }
  useEffect(() => () => hide(), [])
  return (
    <span
      className="c-tip-host"
      aria-describedby={id}
      onMouseEnter={() => show(false)}
      onMouseLeave={hide}
      onFocus={() => show(true)}
      onBlur={hide}
      onKeyDown={(e) => {
        if (e.key === 'Escape') hide()
      }}
    >
      {children}
      {/* Always in the document, so the description is there before it is
          shown; hidden until hover or focus. */}
      <span role="tooltip" id={id} className="c-tip" hidden={!open}>
        {tip}
      </span>
    </span>
  )
}

export interface MenuItem {
  key: string
  label: string
  onSelect?: () => void
  /** Why it cannot be chosen now. Set, the item is disabled and says this. */
  disabledReason?: string
  /** A destructive item, drawn in the failure hue. */
  danger?: boolean
  kbd?: string
  /** A rule above this item. */
  divider?: boolean
}

export function Menu({ label, items, trigger }: { label: string; items: readonly MenuItem[]; trigger?: ReactNode }) {
  const [open, setOpen] = useState(false)
  const host = useRef<HTMLSpanElement | null>(null)
  const listId = useId()
  useEffect(() => {
    if (!open) return
    const onDown = (e: PointerEvent) => {
      if (host.current !== null && e.target instanceof Node && !host.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('pointerdown', onDown)
    return () => document.removeEventListener('pointerdown', onDown)
  }, [open])
  return (
    <span
      className="c-menu-host"
      ref={host}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && open) {
          e.stopPropagation()
          setOpen(false)
          host.current?.querySelector<HTMLButtonElement>('button')?.focus()
        }
      }}
    >
      <Button aria-expanded={open} aria-controls={open ? listId : undefined} onClick={() => setOpen((v) => !v)}>
        {trigger ?? label}
      </Button>
      {open && (
        <div className="c-menu" id={listId} role="group" aria-label={label}>
          {items.map((it) => (
            <Fragment key={it.key}>
              {it.divider === true && <hr />}
              <button
                type="button"
                className={`c-menu-item${it.danger === true ? ' is-bad' : ''}`}
                disabled={it.disabledReason !== undefined}
                onClick={() => {
                  setOpen(false)
                  it.onSelect?.()
                }}
              >
                {it.label}
                {it.kbd !== undefined && <kbd>{it.kbd}</kbd>}
                {it.disabledReason !== undefined && <em>{it.disabledReason}</em>}
              </button>
            </Fragment>
          ))}
        </div>
      )}
    </span>
  )
}

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

export function Dialog({
  title,
  onClose,
  children,
  actions,
}: {
  title: string
  /** Escape, the scrim and the cancel action all close through this. */
  onClose: () => void
  children: ReactNode
  /** The buttons, cancel first, the verb last (right). */
  actions: ReactNode
}) {
  const titleId = useId()
  const box = useRef<HTMLDivElement | null>(null)
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const el = box.current
    // The first field if there is one, else the first control: a typed
    // confirm opens with its field focused.
    const first = el?.querySelector<HTMLElement>('input:not([disabled])') ?? el?.querySelector<HTMLElement>(FOCUSABLE) ?? null
    first?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        closeRef.current()
        return
      }
      if (e.key !== 'Tab' || el === null) return
      const items = Array.from(el.querySelectorAll<HTMLElement>(FOCUSABLE))
      if (items.length === 0) return
      const a = items[0]!
      const z = items[items.length - 1]!
      if (e.shiftKey && (document.activeElement === a || !el.contains(document.activeElement))) {
        e.preventDefault()
        z.focus()
      } else if (!e.shiftKey && (document.activeElement === z || !el.contains(document.activeElement))) {
        e.preventDefault()
        a.focus()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      opener?.focus()
    }
  }, [])
  const dialog = (
    <div
      className="c-scrim"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) closeRef.current()
      }}
    >
      <div className="c-dlg" role="dialog" aria-modal="true" aria-labelledby={titleId} ref={box}>
        <h2 id={titleId}>{title}</h2>
        {children}
        <div className="c-acts">{actions}</div>
      </div>
    </div>
  )
  return typeof document === 'undefined' ? dialog : createPortal(dialog, document.body)
}

/**
 * THE TYPED CONFIRM. `name` is what must be typed, exactly; `verb` is the
 * destructive button's label ("Cancel the workflow", "Set to 0"). `onConfirm`
 * runs only on an exact match, and `busy` keeps the dialog up while the
 * request is in flight so the person sees it take.
 */
export function TypedConfirm({
  title,
  name,
  verb,
  keep,
  onConfirm,
  onClose,
  busy = false,
  error,
  children,
}: {
  title: string
  name: string
  verb: string
  /** The cancel action's words ("Keep it running"). */
  keep: string
  onConfirm: () => void
  onClose: () => void
  busy?: boolean | string
  /** A refusal from the API, said in place (failures never toast). */
  error?: ReactNode
  /** What happens, what does not, and what it frees. */
  children: ReactNode
}) {
  const [typed, setTyped] = useState('')
  const fieldId = useId()
  const hintId = useId()
  const match = typed === name
  const submit = useCallback(() => {
    if (typed === name) onConfirm()
  }, [typed, name, onConfirm])
  return (
    <Dialog
      title={title}
      onClose={onClose}
      actions={
        <>
          <Button kind="ghost" onClick={onClose}>
            {keep}
          </Button>
          <Button kind="danger-filled" disabled={!match} busy={busy} onClick={submit}>
            {verb}
          </Button>
        </>
      }
    >
      {children}
      <div className="c-field">
        <label className="c-lbl" htmlFor={fieldId}>
          Type <code>{name}</code> to confirm
        </label>
        <input
          id={fieldId}
          className="c-inp is-mono"
          value={typed}
          autoComplete="off"
          spellCheck={false}
          aria-describedby={hintId}
          onChange={(e) => setTyped(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              submit()
            }
          }}
        />
        <p className="c-hint" id={hintId}>
          {match ? 'The name matches.' : 'The button unlocks when the name matches exactly.'}
        </p>
      </div>
      {error !== undefined && error !== null && (
        <p className="c-hint" role="alert">
          {error}
        </p>
      )}
    </Dialog>
  )
}

/**
 * CANCEL A WORKFLOW WHILE A STEP RUNS (states.html C, §12, picked
 * 2026-10-02): the typed confirm, with the workflow's id to type. Exported for
 * Workflows.tsx, whose lane wires the call site; while no step is running the
 * two-click inline confirm there stays (`cancelNeedsTypedConfirm`).
 *
 * The sentence names what happens, what does not, and what is kept -- and
 * the counts are the caller's reading of the steps, so an unknown count is
 * passed as null and said as unknown, never as 0.
 */
export function cancelNeedsTypedConfirm(running: number | null): boolean {
  // Unknown is treated as running: the typed confirm is the safe side.
  return running === null || running > 0
}

export function CancelWorkflowConfirm({
  workflowId,
  running,
  notStarted,
  onConfirm,
  onClose,
  busy = false,
  error,
}: {
  workflowId: string
  /** Steps holding capacity now; null when the steps were not read. */
  running: number | null
  /** Steps not yet started; null when the steps were not read. */
  notStarted: number | null
  onConfirm: () => void
  onClose: () => void
  busy?: boolean | string
  error?: ReactNode
}) {
  const steps = (n: number | null, one: string, many: string) => (n === null ? `an unknown number of ${many}` : `${n} ${n === 1 ? one : many}`)
  return (
    <TypedConfirm
      title="Cancel this workflow?"
      name={workflowId}
      verb="Cancel the workflow"
      keep="Keep it running"
      onConfirm={onConfirm}
      onClose={onClose}
      busy={busy === true ? 'Cancelling…' : busy}
      error={error}
    >
      <p>
        {steps(running, 'running step is', 'running steps are')} asked to stop at their next heartbeat;{' '}
        {steps(notStarted, 'step not yet started is', 'steps not yet started are')} cancelled. Steps that finished keep their results.
      </p>
    </TypedConfirm>
  )
}
