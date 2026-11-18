/**
 * THE APP-WIDE STATES (states.html Variant C, the owner's pick of 2026-10-02):
 * three tiers, decided by how far a state reaches.
 *
 *   WHOLE APP  session expired, tenant unresolved, wrong domain or disabled
 *              tenant, and unreachable before anything was read. The CONTENT
 *              AREA is replaced by one panel with the cause, the "says nothing
 *              about what is running" line and the one action; the spine, the
 *              panel and Submit stay usable, so people can still move.
 *   PAGE       degraded, 429 reads paused, unreachable with older data, admin
 *              only, not found: one banner over the page's dimmed older data.
 *              That tier is `Screen`'s (Shell.tsx), which owns the stale rule.
 *   REGION     a card or a table shows its own mark in place (CardSkeleton,
 *              CardFailed, the region's own empty state).
 *
 * Plus the two the states lane proposed: an OFFLINE banner from
 * `navigator.onLine`, so "your browser is offline" and "the API is down" no
 * longer read the same; and the hidden-tab line, which is `Screen`'s.
 *
 * WHICH KINDS ARE FATAL is one list, `WHOLE_APP_KINDS`, beside fetch.ts's
 * classifier and Shell.tsx's NEEDS_A_PERSON. A state that fails every read
 * is a different fact from one that fails one read; this is the one place
 * that decides which is which.
 */
import { useEffect, useState, useSyncExternalStore } from 'react'
import { Banner, Button } from './components'
import { errorHeading, errorReassurance, type ApiError, type ApiErrorKind, type ProbeRecord, type Result } from './fetch'
import type { Me } from './types'

/**
 * The kinds that reach the whole app when the FRAME's identity read meets
 * them: every read the console makes carries the same session, domain and
 * tenant, so the next one would meet them too.
 */
export const WHOLE_APP_KINDS: ReadonlySet<ApiErrorKind> = new Set<ApiErrorKind>([
  'session_expired',
  'unauthenticated',
  'wrong_domain',
  'tenant_disabled',
  'tenant_unresolved',
])

/**
 * The kinds that reach the whole app when ANY read is the newest to meet
 * them. A session that expired, a domain refused, a tenant disabled: a page
 * read finds out first, and the frame's identity read is not asked again.
 * Tenant unresolved is not here -- it is retried and usually clears, so a
 * page read meeting it is that page's banner.
 */
const SESSION_KINDS: ReadonlySet<ApiErrorKind> = new Set<ApiErrorKind>(['session_expired', 'unauthenticated', 'wrong_domain', 'tenant_disabled'])

/** Copy for a fault read off the probe registry, which keeps the kind and not the message. */
const PROBE_MESSAGE: Readonly<Partial<Record<ApiErrorKind, string>>> = {
  session_expired: 'The API answered with a page instead of data, which is how an expired sign-in arrives. Reload to sign in again.',
  unauthenticated: 'The API answered 401: this browser is no longer signed in. Reload to sign in again.',
  wrong_domain: 'The API refused this account: its domain is not permitted to use this deployment.',
  tenant_disabled: 'The API refused this tenant: it is disabled.',
}

/**
 * The whole-app fault, or null.
 *
 * 1. The frame's identity read (`/v1/tenants/me`) failed with a whole-app
 *    kind: nothing on any page can be read as this person.
 * 2. It failed UNREACHABLE and no read in this tab has ever landed: there is
 *    no older data to dim, so a page banner would sit over nothing. With
 *    older data in hand, unreachable is a page state (the stale banner).
 * 3. The NEWEST read of any route met an expired session or a refused domain
 *    or tenant.
 */
export function wholeAppFault(me: Result<Me>, probes: readonly ProbeRecord[]): ApiError | null {
  if (me.status === 'error') {
    if (WHOLE_APP_KINDS.has(me.error.kind)) return me.error
    if (me.error.kind === 'unreachable' && probes.every((p) => p.lastSuccessAt === null)) return me.error
  }
  let newest: ProbeRecord | null = null
  for (const p of probes) if (newest === null || p.lastAttemptAt > newest.lastAttemptAt) newest = p
  if (newest !== null && newest.lastKind !== null && SESSION_KINDS.has(newest.lastKind)) {
    return {
      kind: newest.lastKind,
      httpStatus: newest.lastStatus,
      code: null,
      message: PROBE_MESSAGE[newest.lastKind] ?? errorHeading({ kind: newest.lastKind, httpStatus: null, code: null, message: '' }),
    }
  }
  return null
}

// ---------------------------------------------------------------------------
// Online / offline
// ---------------------------------------------------------------------------

function subscribeOnline(fn: () => void): () => void {
  globalThis.addEventListener?.('online', fn)
  globalThis.addEventListener?.('offline', fn)
  return () => {
    globalThis.removeEventListener?.('online', fn)
    globalThis.removeEventListener?.('offline', fn)
  }
}

/** `navigator.onLine`, true where there is no navigator to ask (it claims nothing). */
function onlineNow(): boolean {
  return typeof navigator === 'undefined' || navigator.onLine !== false
}

export function useOnline(): boolean {
  return useSyncExternalStore(subscribeOnline, onlineNow, () => true)
}

/**
 * THIS DEVICE IS OFFLINE (states.html §13). Named as the device, not the
 * platform: the request never left the browser, so it says nothing about
 * what is running, and reads resume by themselves when it comes back.
 */
export function OfflineBanner() {
  const [since] = useState(() => Date.now())
  return (
    <div className="app-banner">
      <Banner tone="warn" title="Your browser is offline — showing older data">
        Since {new Date(since).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}: navigator.onLine is false. The request never left this device. Reads resume when the connection returns.
      </Banner>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The takeover
// ---------------------------------------------------------------------------

/** What the one action of a takeover is, by kind. */
function actionOf(kind: ApiErrorKind): 'reload' | 'retry' | 'none' {
  if (kind === 'session_expired' || kind === 'unauthenticated') return 'reload'
  if (kind === 'wrong_domain' || kind === 'tenant_disabled') return 'none'
  return 'retry'
}

/**
 * ONE PANEL IN PLACE OF THE PAGE, with the nav still usable. The heading is
 * the classifier's, the sentence the server's, and the reassurance the
 * invariant: this is a failure to read, and says nothing about what runs.
 *
 * Tenant unresolved is AMBER, not red: it is retried with back-off by the
 * frame (`useFrameRead`) and usually clears. A takeover never flashes for a
 * blip: unreachable takes over only before anything was read.
 */
export function AppTakeover({ error, online, onRetry }: { error: ApiError; online: boolean; onRetry: () => void }) {
  const act = actionOf(error.kind)
  const tone = error.kind === 'tenant_unresolved' ? 'is-warn' : 'is-bad'
  const offline = error.kind === 'unreachable' && !online
  return (
    <section className={`app-takeover ${tone}`} role="alert" aria-labelledby="app-takeover-h" data-kind={error.kind}>
      <i className="ctl-mark is-unread">not read</i>
      <h1 id="app-takeover-h">{offline ? 'Your browser is offline' : errorHeading(error)}</h1>
      <p>{offline ? 'navigator.onLine is false, so no request left this device.' : error.message}</p>
      <p className="app-takeover-inv">Nothing on this page is a reading of the platform. {errorReassurance(error)}</p>
      {act === 'reload' && (
        <Button kind="primary" onClick={() => window.location.reload()}>
          Reload to sign in
        </Button>
      )}
      {act === 'retry' && (
        <Button kind="primary" onClick={onRetry}>
          {error.kind === 'tenant_unresolved' ? 'Try now' : 'Try again'}
        </Button>
      )}
      <p className="app-takeover-foot">
        {error.httpStatus === null ? 'no response' : `HTTP ${error.httpStatus}`}
        {error.code === null ? '' : ` · ${error.code}`}
        {act === 'retry' ? ' · retried on its own, backing off' : act === 'reload' ? ' · reads stopped until you sign in' : ' · nothing here can change it; an admin can'}
      </p>
    </section>
  )
}

// ---------------------------------------------------------------------------
// The hidden-tab line (states.html §13)
// ---------------------------------------------------------------------------

/**
 * How long a tab must have been hidden before the line is worth saying. A
 * glance at another tab does not stop a read falling due; a minute does
 * (`Screen` disarms its poll while hidden).
 */
export const HIDDEN_LINE_AFTER_MS = 60_000

/**
 * "This tab was hidden for 6m, so it stopped reading. Reading now…" -- from
 * the moment the tab comes back until `reading` goes false. `wasHiddenMs` is
 * how long it was away; null when it was not.
 */
export function HiddenTabLine({ wasHiddenMs, reading }: { wasHiddenMs: number | null; reading: boolean }) {
  if (wasHiddenMs === null || !reading) return null
  const mins = Math.max(1, Math.round(wasHiddenMs / 60_000))
  return (
    <p className="app-hidden-line" role="status">
      This tab was hidden for {mins}m, so it stopped reading. Reading now&hellip;
    </p>
  )
}

/**
 * Tracks the tab's hidden spells: the length of the last one that ended, until
 * `reset` is called (by the read that landed).
 */
export function useHiddenSpell(): { wasHiddenMs: number | null; reset: () => void } {
  const [wasHiddenMs, setWas] = useState<number | null>(null)
  useEffect(() => {
    if (typeof document === 'undefined') return
    let hiddenAt: number | null = document.hidden ? Date.now() : null
    const onVis = () => {
      if (document.hidden) {
        hiddenAt = Date.now()
        return
      }
      if (hiddenAt === null) return
      const away = Date.now() - hiddenAt
      hiddenAt = null
      if (away >= HIDDEN_LINE_AFTER_MS) setWas(away)
    }
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [])
  return { wasHiddenMs, reset: () => setWas(null) }
}
