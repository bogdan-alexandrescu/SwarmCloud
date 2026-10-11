/**
 * The pieces Work › Repositories and Git tokens share: reading a region,
 * saying a route is not served yet, the freshness pill, the capability mark,
 * the breadcrumb and a routed link. Built LOCALLY, prefixed `Ur`, on the
 * canonical set that is on main (components.html A); lane U0's set is landing
 * in parallel, and a later pass can swap each of these for its own by name.
 */
import { useCallback, useEffect, useRef, useState, type MouseEvent, type ReactNode } from 'react'
import { Button, ButtonLink, Dash, EmptyState, LoadingState, routedClick, type ButtonKind, type ButtonSize } from './components'
import { MarkGlyph } from './marks'
import { AGED_AFTER_MS, FailedPanel, RefreshControl, useClaimPageAge } from './Shell'
import type { ApiError, Result } from './fetch'
import { addressToPath } from './paths'
import { AGE_TICK_MS, useNow } from './useNow'
import { CAPABILITIES, coverageDetail, coverageRatio, coverageWords, notServed, pct, type CapCell, type CapRow, type Freshness, type RepoRecord } from './RepositoriesData'

/**
 * One read, re-run on `reload()` and whenever `key` changes. A late answer for an old key is dropped.
 *
 * A RELOAD THAT FAILS WITH DATA IN HAND IS `stale`, NEVER `error` (fetch.ts's
 * `Result` rule). `readAs` reads without `previous`, so this hook keeps it:
 * before, a background re-read that hit a passing 409 (swarm-api answers one
 * while a GitHub token refresh holds its lease) swapped the region for the
 * failure panel and unmounted everything inside it -- a typed owner/repo, a
 * pending Enable and its answer (QA of /access, 2026-10-08). Only the SAME
 * key's data is kept: another key's is another question's answer.
 */
export function useUrRead<T>(load: () => Promise<Result<T>>, key: string): UrRead<T> {
  const [state, setState] = useState<Result<T>>({ status: 'loading', since: Date.now() })
  const [nonce, setNonce] = useState(0)
  const heldKey = useRef<string | null>(null)
  useEffect(() => {
    let live = true
    setState((was) => (was.status === 'ok' || was.status === 'stale' ? was : { status: 'loading', since: Date.now() }))
    void load().then((r) => {
      if (!live) return
      setState((was) => {
        if (r.status === 'error' && heldKey.current === key && (was.status === 'ok' || was.status === 'stale')) {
          return { status: 'stale', data: was.data, fetchedAt: was.fetchedAt, error: r.error }
        }
        heldKey.current = r.status === 'ok' || r.status === 'stale' ? key : null
        return r
      })
    })
    return () => {
      live = false
    }
    // `load` is a new closure every render; `key` names what it reads.
  }, [key, nonce])
  const reload = useCallback(() => setNonce((n) => n + 1), [])
  return { state, reload }
}

/** What `useUrRead` hands back: the read, and the way to run it again. */
export type UrRead<T> = { state: Result<T>; reload: () => void }

/**
 * THE PAGE'S REFRESH, CARRYING ITS ONE TICKING AGE (#98, #138; owner rulings
 * 2026-10-07). These pages read through `useUrRead`, not `Screen`, so they
 * had no refresh in the head and the frame drew its plain `newest read`
 * span there instead. Every screen's own age lives on the control that renews
 * it, so this is `Screen`'s `RefreshControl` over the page's head reads: the
 * age is the newest of them, `not refreshed` once any is a failed refresh's or
 * older than `AGED_AFTER_MS`, and pressing it re-runs every one. It claims the
 * page age, so the frame prints none beside it: one age per screen. These
 * pages are read once per visit, so it carries no cadence (#117 names none).
 */
export function UrRefresh({ reads }: { reads: readonly UrRead<unknown>[] }) {
  useClaimPageAge(true)
  const now = useNow(AGE_TICK_MS)
  const ats = reads.flatMap((r) => (r.state.status === 'loading' || r.state.status === 'error' ? [] : [r.state.fetchedAt]))
  const readAt = ats.length === 0 ? null : Math.max(...ats)
  return (
    <RefreshControl
      readAt={readAt}
      now={now}
      stale={reads.some((r) => r.state.status === 'stale') || (readAt !== null && now - readAt > AGED_AFTER_MS)}
      reading={reads.some((r) => r.state.status === 'loading')}
      onRefresh={() => {
        for (const r of reads) r.reload()
      }}
    />
  )
}

/**
 * A ROUTE THE API DOES NOT SERVE YET (the backend lanes RI1, RI2 and GT1 are
 * building them in parallel). Said in place, so nobody reads an absent region
 * as an empty one -- and never as a failure of something that exists. The
 * route is named on the element (`data-notserved` and its title), not in the
 * visible words: an API path is not copy for a user's screen (walkthrough E,
 * owner 2026-10-03). `what` is the sentence's subject and `plural` its
 * number, so a plural subject reads "Index runs are not served", never
 * "is" (QA G4-20).
 */
export function UrNotServed({ route, what, plural = false }: { route: string; what: string; plural?: boolean }) {
  return (
    <div className="ur-notserved" data-notserved={route} title={`Not served: ${route}`}>
      <EmptyState kind="partial" heading="Not served yet">
        {what} {plural ? 'are' : 'is'} not served by this API yet. Nothing here is a zero: it has not been read.
      </EmptyState>
    </div>
  )
}

/**
 * A region's read, in its states: reading, not served yet, failed (with
 * Retry), empty, or its data. A stale read keeps the data, dimmed, with the
 * failure above it (states.html C, page tier).
 */
export function UrRegion<T>({
  state,
  route,
  what,
  onRetry,
  empty,
  lines = 3,
  alsoNotServed,
  plural = false,
  reading,
  children,
}: {
  state: Result<T>
  /** The route as the design names it: `GET /v1/repositories/{repo_id}/languages`. */
  route: string
  /** What the region shows, as the subject of "… comes from <route>". */
  what: string
  onRetry: () => void
  empty?: ReactNode
  lines?: number
  /** A failure this one route answers when it is not there yet, beyond `notServed`'s. */
  alsoNotServed?: (e: ApiError) => boolean
  /** `what` is plural: "… are not served" (QA G4-20). */
  plural?: boolean
  /** Said in words while the first read runs, for a read slow enough that a bare skeleton reads as broken. */
  reading?: ReactNode
  children: (data: T) => ReactNode
}) {
  if (state.status === 'loading') {
    if (reading === undefined) return <LoadingState lines={lines} label={`Reading ${what.toLowerCase()}…`} />
    return (
      <div className="ur-reading">
        <p className="ur-reading-why" role="status">
          {reading}
        </p>
        <LoadingState lines={lines} label={`Reading ${what.toLowerCase()}…`} />
      </div>
    )
  }
  if (state.status === 'error') {
    if (notServed(state.error) || alsoNotServed?.(state.error) === true) return <UrNotServed route={route} what={what} plural={plural} />
    return <FailedPanel error={state.error} onRetry={onRetry} />
  }
  if (state.status === 'empty') return <>{empty ?? null}</>
  // ONE TREE FOR A LIVE AND A STALE READ. These used to be two shapes (a bare
  // fragment, and the data inside two divs), so a re-read going stale or
  // recovering remounted the region: every control in it lost its state.
  // `.ur-held` is `display: contents` until it dims, so the live shape lays
  // out exactly as the bare fragment did.
  const stale = state.status === 'stale'
  return (
    <>
      {stale ? (
        <p className="ur-stale-why" role="status">
          The last read failed ({state.error.message}); this is the previous answer.
        </p>
      ) : null}
      <div className={stale ? 'ur-held ur-dim' : 'ur-held'}>{children(state.data)}</div>
    </>
  )
}

/** The freshness pill (components.html A `c-pill`), with its reason as its title. */
export function UrFreshPill({ f, sha }: { f: Freshness; sha?: string | null }) {
  return (
    <span className={`c-pill is-${f.hue}`} data-fresh={f.kind} data-mark={f.mark} title={f.why ?? undefined}>
      <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
        <MarkGlyph mark={f.mark} />
      </svg>
      {sha ? `${f.word} · ${sha}` : f.word}
    </span>
  )
}

const CAP_MARK = { ok: 'succeeded', missing: 'failed', unknown: 'queued' } as const

/**
 * One capability (git-tokens.md §5): ok (measured), missing (refused, with
 * the reason) or unknown (not readable without trying), with its reason as
 * the title. An unknown is the grey ring, never green.
 */
export function UrCap({ label, cell, word }: { label: string; cell: CapCell; word?: string }) {
  const reason = cell.reason ?? (cell.state === 'unknown' ? 'Not served for this pair' : null)
  return (
    <span className={`ur-cap is-${cell.state}`} data-cap={cell.state} title={`${label}: ${cell.state}${reason ? `. ${reason}` : ''}`}>
      <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
        <MarkGlyph mark={CAP_MARK[cell.state]} />
      </svg>
      {word ?? label}
    </span>
  )
}

/** The eight capabilities as one row of marks (Settings A's resolved token). */
export function UrCapRow({ row }: { row: CapRow }) {
  return (
    <div className="ur-tokrow">
      {CAPABILITIES.map((c) => (
        <UrCap key={c.key} label={c.label} cell={row[c.key]} />
      ))}
    </div>
  )
}

/** A link to a router address: a real href for a new tab, `go()` for a plain click. */
export function UrLink({
  to,
  go,
  className,
  children,
}: {
  to: string
  go: (to: string) => void
  className?: string
  children: ReactNode
}) {
  return (
    <a
      className={className}
      href={addressToPath(to)}
      onClick={(e: MouseEvent) => {
        if (!routedClick(e)) return
        e.preventDefault()
        go(to)
      }}
    >
      {children}
    </a>
  )
}

/** The frames' breadcrumb: `Work › Repositories › <here>`, every step but the last a link. */
export function UrCrumb({ trail, go }: { trail: readonly { label: string; to?: string }[]; go: (to: string) => void }) {
  return (
    <nav className="ur-crumb" aria-label="Breadcrumb">
      {trail.map((t, i) => (
        <span key={`${i}:${t.label}`}>
          {i > 0 && <span aria-hidden> › </span>}
          {t.to === undefined ? i === trail.length - 1 ? <b>{t.label}</b> : t.label : <UrLink to={t.to} go={go}>{t.label}</UrLink>}
        </span>
      ))}
    </nav>
  )
}

export const LIST = 'work/repositories'
export const REGISTER = 'work/repositories?page=register'
export const GT_PAGE = 'work/repositories?page=tokens'
export const PERMISSIONS = 'work/repositories?page=permissions'

/** The address of one repository, or one of its tabs. */
export function repoAddress(repoId: string, tab: string | null = null, pr: number | null = null): string {
  const q = new URLSearchParams({ repo: repoId })
  if (tab !== null && tab !== 'overview') q.set('tab', tab)
  // The run page's PR card opens Impact on its pull request (screen 9).
  if (tab === 'impact' && pr !== null) q.set('pr', String(pr))
  return `${LIST}?${q.toString()}`
}

/**
 * The Graph tab opened on its "Tests reaching a symbol" view, searching `q`
 * (QA G4-09): the Test map tab answers path -> tests and links each of its
 * rows here, where the question is symbol -> tests.
 */
export function graphTestsAddress(repoId: string, q: string): string {
  return `${LIST}?${new URLSearchParams({ repo: repoId, tab: 'graph', view: 'tests', q }).toString()}`
}

/** A navigation drawn as a button (`ButtonLink`), routed like `UrLink`. */
export function UrNavButton({
  to,
  go,
  kind = 'secondary',
  size,
  children,
}: {
  to: string
  go: (to: string) => void
  kind?: ButtonKind
  size?: ButtonSize
  children: ReactNode
}) {
  return (
    <ButtonLink
      kind={kind}
      size={size}
      href={addressToPath(to)}
      onClick={(e) => {
        if (!routedClick(e)) return
        e.preventDefault()
        go(to)
      }}
    >
      {children}
    </ButtonLink>
  )
}

/**
 * A single choice drawn as the canonical segmented control (`c-seg`, whose
 * `aria-checked` state it already styles), with radio semantics: the frames'
 * `.radio` rows (schedule, scope, policy) are one-of-N choices, not toggles.
 */
export function UrRadio<K extends string>({
  label,
  options,
  value,
  onChange,
  disabled = false,
}: {
  label: string
  options: readonly { key: K; label: ReactNode; title?: string; disabled?: boolean }[]
  value: K | null
  onChange?: (k: K) => void
  disabled?: boolean
}) {
  return (
    // A whole group disabled is a value shown, not a choice refused: drawn read-only, at full contrast (V046).
    <div className={disabled ? 'c-seg ur-radio is-readonly' : 'c-seg ur-radio'} role="radiogroup" aria-label={label} aria-readonly={disabled || undefined}>
      {options.map((o) => (
        <button
          key={o.key}
          type="button"
          role="radio"
          aria-checked={o.key === value}
          title={o.title}
          disabled={disabled || o.disabled === true || undefined}
          onClick={() => onChange?.(o.key)}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

/**
 * What a refused write said, or -- when its route is not there yet -- that
 * in plain words. The route stays out of the sentence (walkthrough E).
 */
export function writeFailure(e: ApiError): string {
  return notServed(e) ? 'Not served yet: this API does not take this request yet.' : e.message
}

/** The test-map bar: filled to the ratio, or hatched when there is nothing measured. */
export function UrBar({ ratio, label }: { ratio: number | null; label: string }) {
  if (ratio === null) return <span className="ur-bar is-unmeasured" role="img" aria-label={label} />
  return (
    <span className="ur-bar" role="img" aria-label={label}>
      <i style={{ width: pct(ratio) }} />
    </span>
  )
}

/** Why "Tests mapped" is a dash, in the words its tooltip carries. */
export function testsMappedWhy(r: RepoRecord): string {
  if (r.index.current_sha === null) return 'No index has been built yet'
  return r.index.coverage === null
    ? 'The registration carries no test-map coverage for its index'
    : 'The index did not report how many of its modules have a test edge'
}

/**
 * "Tests mapped" with its bar: the index's MODULES with at least one test
 * edge, with the edges and always-run tests behind the figure -- the counts
 * `index.coverage` serves (QA G4-03, repo-index.md §6.2). Never a percentage
 * of source files: the index does not count those.
 */
export function TestsMapped({ r }: { r: RepoRecord }) {
  const c = r.index.coverage
  const words = coverageWords(c)
  const detail = coverageDetail(c)
  const why = testsMappedWhy(r)
  return (
    <div className="ur-tm">
      <span className="ur-mu">Tests mapped</span>
      <UrBar ratio={coverageRatio(c)} label={words === null ? `Tests mapped: ${why}` : `Tests mapped: ${words} have a test edge`} />
      {words === null ? <Dash why={why} /> : <b>{words}</b>}
      {detail !== null && <small className="ur-tm-sub">{detail}</small>}
    </div>
  )
}

/**
 * Copy a command, with the command as the button's title and "Copied" once
 * it is on the clipboard. A refused clipboard leaves the button as it was:
 * the command is on the page beside it to select by hand.
 */
export function UrCopy({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }
  return (
    <Button size="sm" kind="ghost" className="ur-copy" aria-label={label} title={text} onClick={() => void copy()}>
      {copied ? 'Copied' : 'Copy'}
    </Button>
  )
}

/** How far into a strip's edge a fade reaches: the room left beside a tab scrolled into view. */
export const FADE_PX = 32

/**
 * Which edges of a horizontally scrolling strip have more behind them (QA
 * G4-19): at 390 the repository tabs scroll and three of eight are off the
 * right edge, so that edge fades -- the affordance a bare overflow lacks.
 */
export function stripFade(s: { scrollLeft: number; scrollWidth: number; clientWidth: number }): 'none' | 'start' | 'end' | 'both' {
  const start = s.scrollLeft > 1
  const end = s.scrollLeft + s.clientWidth < s.scrollWidth - 1
  return start && end ? 'both' : start ? 'start' : end ? 'end' : 'none'
}

/**
 * The scroll offset that shows `item` (its left in the strip's content, and
 * its width) with the fade's room beside it; null when it is already in view.
 * Set on the strip itself, so the page never jumps the way `scrollIntoView`
 * would.
 */
export function scrollToShow(strip: { scrollLeft: number; clientWidth: number }, item: { left: number; width: number }): number | null {
  if (item.left < strip.scrollLeft) return Math.max(0, item.left - FADE_PX)
  if (item.left + item.width > strip.scrollLeft + strip.clientWidth) return item.left + item.width - strip.clientWidth + FADE_PX
  return null
}
