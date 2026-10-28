/**
 * The pieces Work › Repositories and Git tokens share: reading a region,
 * saying a route is not served yet, the freshness pill, the capability mark,
 * the breadcrumb and a routed link. Built LOCALLY, prefixed `Ur`, on the
 * canonical set that is on main (components.html A); lane U0's set is landing
 * in parallel, and a later pass can swap each of these for its own by name.
 */
import { Fragment, useCallback, useEffect, useRef, useState, type MouseEvent, type ReactNode } from 'react'
import { Banner, Button, ButtonLink, Dash, EmptyState, LoadingState, routedClick, type ButtonKind, type ButtonSize } from './components'
import { MarkGlyph } from './marks'
import { AGED_AFTER_MS, FailedPanel, RefreshControl, useClaimPageAge } from './Shell'
import type { ApiError, Result } from './fetch'
import { addressToPath } from './paths'
import { AGE_TICK_MS, useNow } from './useNow'
import { RepoGraphCache } from './RepoGraphCache'
import { CAPABILITIES, coverageDetail, coverageRatio, coverageWords, notServed, pct, shortSha, thousands, type CapCell, type CapRow, type Freshness, type RepoRecord } from './RepositoriesData'

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
export function UrRefresh({ reads, ageOf = reads }: { reads: readonly UrRead<unknown>[]; ageOf?: readonly UrRead<unknown>[] }) {
  useClaimPageAge(true)
  const now = useNow(AGE_TICK_MS)
  // THE AGE IS THE PAGE'S OWN READ'S (V223). `ageOf` names the reads the
  // page stands on: a repository whose registration is still pending, or
  // failed, said "read 0 s ago" from the index read beside it on this control.
  const ats = ageOf.flatMap((r) => (r.state.status === 'loading' || r.state.status === 'error' ? [] : [r.state.fetchedAt]))
  const readAt = ats.length === 0 ? null : Math.max(...ats)
  return (
    <RefreshControl
      readAt={readAt}
      now={now}
      stale={ageOf.some((r) => r.state.status === 'stale') || (readAt !== null && now - readAt > AGED_AFTER_MS)}
      reading={reads.some((r) => r.state.status === 'loading')}
      onRefresh={() => {
        for (const r of reads) r.reload()
      }}
    />
  )
}

/**
 * `what` opens the sentence, so its first letter is a capital (visual QA
 * QA-062, 2026-10-20): callers write it as the subject of "Reading …" too,
 * where it is lower case, and "the Jev backend is not served" opened a notice
 * in lower case. Only the first letter: "Jev" and "Code intelligence" keep theirs.
 */
function sentenceCase(what: string): string {
  return what.charAt(0).toUpperCase() + what.slice(1)
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
        {sentenceCase(what)} {plural ? 'are' : 'is'} not served by this API yet. Nothing here is a zero: it has not been read.
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

/** The longest label a segment of a row of three or more holds before the row is drawn as a stack (V220). */
export const SEGMENT_WORDS = 32

/**
 * Whether a group's options are sentences rather than segment words (V220):
 * three or more options, one longer than `SEGMENT_WORDS`. The change trigger's
 * "Webhook · on push to the default branch" wrapped its row in a half-width
 * card at 1440, leaving "Off" alone on a second row under a divider.
 */
export function stacksOf(options: readonly { label: ReactNode }[]): boolean {
  return options.length > 2 && options.some((o) => typeof o.label === 'string' && o.label.length > SEGMENT_WORDS)
}

/**
 * A single choice drawn as the canonical segmented control (`c-seg`, whose
 * `aria-checked` state it already styles), with radio semantics: the frames'
 * `.radio` rows (schedule, scope, policy) are one-of-N choices, not toggles.
 * Options that are sentences are one segment per line (`is-stack`, V220).
 */
export function UrRadio<K extends string>({
  label,
  options,
  value,
  onChange,
  disabled = false,
  stack = stacksOf(options),
}: {
  label: string
  options: readonly { key: K; label: ReactNode; title?: string; disabled?: boolean }[]
  value: K | null
  onChange?: (k: K) => void
  disabled?: boolean
  /** One segment per line; by default when the options are sentences (`stacksOf`). */
  stack?: boolean
}) {
  const cls = ['c-seg', 'ur-radio', stack ? 'is-stack' : null, disabled ? 'is-readonly' : null].filter(Boolean).join(' ')
  return (
    // A whole group disabled is a value shown, not a choice refused: drawn read-only, at full contrast (V046).
    <div className={cls} role="radiogroup" aria-label={label} aria-readonly={disabled || undefined}>
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

/**
 * A repository path that breaks only after a `/` (V216): with
 * `overflow-wrap: anywhere` the Modules column cut "swarm_common" into
 * "swarm_comm / on". The text is unchanged; each `/` is followed by a break
 * opportunity, so a narrow column wraps at a directory, never inside a name.
 */
export function SlashBreaks({ path }: { path: string }) {
  const parts = path.split('/')
  return (
    <>
      {parts.map((p, i) => (
        <Fragment key={i}>
          {p}
          {i < parts.length - 1 && (
            <>
              /<wbr />
            </>
          )}
        </Fragment>
      ))}
    </>
  )
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
export function TestsMapped({ r, cut = false }: { r: RepoRecord; cut?: boolean }) {
  const c = r.index.coverage
  const words = coverageWords(c)
  const detail = cutDetail(c, cut)
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

/**
 * THE EDGE COUNT, SAID AS CUT WHEN THE INDEX CUT ITS TEST MAP (live QA
 * RSA-4, 2026-10-11). The indexer keeps at most its ceiling of edges and
 * names `test_map` in the document's `truncated`; "1,000 edges" then reads as
 * a complete count when 1,000 is only where the list stopped. `cut` comes from
 * the index document, so a page that has not read it says what was served.
 */
export function edgesWords(edges: number, cut: boolean): string {
  return cut ? `${thousands(edges)}+ edges (cut)` : `${thousands(edges)} edge${edges === 1 ? '' : 's'}`
}

/** `coverageDetail`, with the edges said as cut when the test map was (RSA-4). */
export function cutDetail(c: RepoRecord['index']['coverage'], cut: boolean): string | null {
  if (!cut || c === null || c.test_map_edges === null) return coverageDetail(c)
  const always = c.always_tests === null ? [] : [`${thousands(c.always_tests)} always-run`]
  return [edgesWords(c.test_map_edges, true), ...always].join(' · ')
}

/**
 * INDEXING SPENDS CAPACITY, SO IT IS CONFIRMED FIRST (live QA RSA-15,
 * 2026-10-11). Index now and the full run each queue a task through the API
 * on one click; the task holds capacity from the moment it is LEASED until it
 * ends (invariants 1 and 3), so a misclick was a run nobody meant. This is the
 * same inline confirmation the Access page uses for a push test, with the safe
 * choice focused and Esc taking it, so a keyboard reader lands on Cancel.
 */
export function UrConfirmIndex({
  name,
  kind,
  base,
  busy,
  onConfirm,
  onCancel,
}: {
  name: string
  kind: 'full' | 'incremental'
  /** The index an incremental run starts from; null when there is none. */
  base: string | null
  busy: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    box.current?.querySelector<HTMLButtonElement>('button[data-safe]')?.focus()
  }, [])
  return (
    <div
      className="ur-confirm"
      ref={box}
      data-confirm={kind}
      onKeyDown={(e) => {
        if (e.key !== 'Escape' || busy) return
        e.stopPropagation()
        onCancel()
      }}
    >
      <Banner
        tone="warn"
        title={`Queue ${kind === 'full' ? 'a full' : 'an incremental'} index run for ${name}?`}
        actions={
          <>
            <Button kind="primary" size="sm" busy={busy} onClick={onConfirm}>
              Queue {kind} run
            </Button>
            <Button size="sm" data-safe="" disabled={busy} onClick={onCancel}>
              Cancel
            </Button>
          </>
        }
      >
        <p className="ur-small">
          {kind === 'full'
            ? 'A full run re-reads every file and re-resolves every call site with the language servers.'
            : `An incremental run re-reads only what changed since the index at ${base === null ? 'its last commit' : shortSha(base)}.`}{' '}
          It is an agent like any other: it holds capacity from the moment it is leased until it finishes.
        </p>
      </Banner>
    </div>
  )
}

/**
 * WHETHER A REPOSITORY HAS INDEXING ON, for its Cache section
 * (graph-answer-cache.md §5.1, CACHE-4-CONSOLE). On when it has an index to
 * cache (`index.current_sha`), or when its schedule builds one: an interval,
 * or a change trigger. A registration with both off and no index has nothing
 * a cache could hold, so it gets no section, not an empty one. An unserved
 * schedule (null) is not a confident off: it counts as on.
 */
export function indexingOn(r: RepoRecord): boolean {
  const ix = r.index
  if (ix.current_sha !== null) return true
  return !(ix.interval_hours === 'off' && ix.on_change === 'off')
}

/**
 * The Cache section in a repository's card when indexing is on (§5.1): folded,
 * and reading nothing until it is opened. Nothing at all when indexing is off.
 */
export function UrGraphCacheSection({ r }: { r: RepoRecord }) {
  if (!indexingOn(r)) return null
  return <RepoGraphCache repoId={r.repo_id} indexSha={r.index.current_sha} />
}
