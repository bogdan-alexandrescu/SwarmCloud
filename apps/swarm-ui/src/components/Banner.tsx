/**
 * BANNERS AND TOASTS (components.html A, "Toasts and banners"; states.html C).
 *
 * A BANNER is where a FAILURE lives: on screen, next to the data it affects,
 * until the condition clears. "Read 4m ago, not refreshed since. The rows
 * below are dimmed until a read lands." It is never dismissed by a thumb.
 *
 * A TOAST ONLY ACKNOWLEDGES SOMETHING THE VIEWER JUST DID ("Link copied",
 * "Now acting as platform"), and goes after 4s -- paused while the pointer or
 * the keyboard is on it, so its one action can still be reached. There is no
 * failure toast and no way to make one: `acknowledge` has no tone, because a
 * failure that can disappear before it is read is the thing Shell.tsx's
 * stale banner was written to prevent ("at 390pt a toast sits under the
 * thumb and gets dismissed by accident").
 */
import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import { BadGlyph, InfoGlyph, WarnGlyph } from './glyphs'
import { CIcon } from './icons'

export type BannerTone = 'warn' | 'bad' | 'info' | 'neu'

export function Banner({
  tone,
  title,
  children,
  actions,
  role,
}: {
  tone: BannerTone
  title: ReactNode
  /** The facts: since when, what it means, what still works. */
  children?: ReactNode
  /** The one way out: Retry, Reload, Switch back. */
  actions?: ReactNode
  /** `alert` for a failure the reader must hear now; `status` otherwise. */
  role?: 'alert' | 'status'
}) {
  return (
    <div className={`c-banner is-${tone}`} role={role ?? (tone === 'bad' ? 'alert' : 'status')}>
      {tone === 'bad' ? <BadGlyph /> : tone === 'warn' ? <WarnGlyph /> : <InfoGlyph />}
      <div className="c-banner-body">
        <b>{title}</b>
        {children !== undefined && <span>{children}</span>}
      </div>
      {actions !== undefined && <div className="c-banner-acts">{actions}</div>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Toasts: a module store, so any component can acknowledge and one <Toaster>
// draws them.
// ---------------------------------------------------------------------------

/** How long an acknowledgement stays (components.html A: "goes after 4s"). */
export const TOAST_MS = 4000

export interface Toast {
  id: number
  /** What the viewer just did, as a fact. */
  message: ReactNode
  /** One follow-up: "View", "Back to eng". */
  action?: { label: string; onClick: () => void }
}

let toasts: Toast[] = []
let nextId = 1
const listeners = new Set<() => void>()
const publish = () => {
  for (const fn of listeners) fn()
}

/**
 * Acknowledge the viewer's OWN action. Returns the toast's id. There is no
 * tone: a failure is a `Banner`, never this.
 */
export function acknowledge(message: ReactNode, action?: Toast['action']): number {
  const id = nextId++
  toasts = [...toasts, { id, message, action }].slice(-3)
  publish()
  return id
}

export function dismissToast(id: number): void {
  const next = toasts.filter((t) => t.id !== id)
  if (next.length === toasts.length) return
  toasts = next
  publish()
}

/** For tests: start from no toasts. */
export function forgetToasts(): void {
  toasts = []
  publish()
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function Toaster() {
  const list = useSyncExternalStore(subscribe, () => toasts, () => toasts)
  if (list.length === 0) return null
  return (
    <div className="c-toasts" aria-live="polite" role="status">
      {list.map((t) => (
        <ToastRow key={t.id} toast={t} />
      ))}
    </div>
  )
}

function ToastRow({ toast }: { toast: Toast }) {
  const [held, setHeld] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => {
    if (held) return
    timer.current = setTimeout(() => dismissToast(toast.id), TOAST_MS)
    return () => clearTimeout(timer.current)
  }, [held, toast.id])
  return (
    <div
      className="c-toast"
      data-toast={toast.id}
      onMouseEnter={() => setHeld(true)}
      onMouseLeave={() => setHeld(false)}
      onFocus={() => setHeld(true)}
      onBlur={() => setHeld(false)}
    >
      <CIcon name="check" />
      <span>{toast.message}</span>
      {toast.action !== undefined && (
        <button
          type="button"
          className="c-link"
          onClick={() => {
            dismissToast(toast.id)
            toast.action!.onClick()
          }}
        >
          {toast.action.label}
        </button>
      )}
      <button type="button" className="c-x" aria-label="Dismiss" onClick={() => dismissToast(toast.id)}>
        <CIcon name="close" />
      </button>
    </div>
  )
}
