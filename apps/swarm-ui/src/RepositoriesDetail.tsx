import { useEffect, useRef, useState, type ReactNode } from 'react'
import {
  loadRepository, loadRepositoryIndex, loadRepositoryLanguages, loadResolvedToken, patchRepositoryContext, patchRepositoryTrigger, runRepositoryIndex,
  type RepoContext, type RepoDetailRead, type RepoHardStops,
} from './api'
import { Banner, Button, Card, Chip, Dash, EmptyState, NamedMark, StatePill, ToneMark, routedClick } from './components'
import { addressToPath } from './paths'
import {
  coverageWords, freshness, HEAD_UNREAD, intervalWords, kindWords, pct, repoName, shortSha, notServed, expiryWords,
  thousands, TRIGGER_OPTIONS, WEBHOOK_NOTE,
  type ChangeTrigger, type IndexDoc, type IndexRun, type LanguageRow, type RepoDetail, type RepoRecord, type ResolvedToken, type UsedBy,
} from './RepositoriesData'
import type { Result } from './fetch'
import {
  LIST, GT_PAGE, TestsMapped, testsMappedWhy, scrollToShow, stripFade, UrCapRow, UrConfirmIndex, cutDetail, UrCrumb, UrFreshPill, UrLink, UrNavButton, UrNotServed, UrRadio, UrRefresh, UrRegion, SlashBreaks, repoAddress,
  type UrRead,
  useUrRead, writeFailure,
} from './RepositoriesParts'
import { RepoPermissions } from './PermissionCards'
import { PageHead } from './Shell'
import { GraphTab, type GraphOpen } from './RepoGraph'
import { AlwaysRun, EdgeGroups, PathLookup, Suites } from './RepoTestMap'
import { ImpactTab } from './RepoImpact'
import { formatDuration, pluralise, timeAgo, TERMINAL_STATES } from './types'

/**
 * ONE REPOSITORY, pick A (repositories.html screen 4): tabs Overview, Graph
 * and Impact (screens 8 and 9, RepoGraph.tsx and RepoImpact.tsx), Test
 * map, Hot-spots, Index runs, Settings, Used by -- one tab per part of the
 * index -- with Settings drawn as screen 11's pick A: cards for the schedule,
 * the languages, the graph, the selection policy (P3 / X2, PICKS.md) and the
 * git token that resolves here with its capability row; and, where the API
 * serves them, the hard stops (docs/schedules.md §4.4, lane S11): whether the
 * repository is the platform's own and its protected paths, the four
 * defaults marked as the floor a member cannot remove.
 *
 * EVERY FIGURE NAMES THE COMMIT IT DESCRIBES: the meta line carries the index
 * sha and the head sha with when the head was read, and an index behind its
 * head leads with a banner (states.html C, page tier) before anything else.
 *
 * EACH REGION READS ITS OWN ROUTE, so a route not built yet costs only its
 * region: the index document, the languages and the resolved token each say
 * "not served yet" in place, naming their route, while the rest draws.
 *
 * SETTINGS SHOW THE SERVED VALUES AND DO NOT CHANGE THEM, except Code
 * intelligence and the change trigger (HOOKS-UI), which write through
 * `PATCH /v1/repositories/{repo_id}` (repo-index.md §6.1). The other controls
 * are drawn as the frame draws them, disabled, with that reason beside them.
 */

const TABS = [
  { key: 'overview', label: 'Overview' },
  // Screens 8 and 9 (Graph A, Impact A): the frames put them after Overview.
  { key: 'graph', label: 'Graph' },
  { key: 'impact', label: 'Impact' },
  { key: 'test-map', label: 'Test map' },
  { key: 'hot-spots', label: 'Hot-spots' },
  { key: 'index-runs', label: 'Index runs' },
  { key: 'settings', label: 'Settings' },
  { key: 'used-by', label: 'Used by' },
] as const
type TabKey = (typeof TABS)[number]['key']

function tabOf(t: string | null): TabKey {
  return (TABS.find((x) => x.key === t)?.key ?? 'overview') as TabKey
}

/** The route these settings change through; named on the element, not in the words (walkthrough E). */
const PATCH_ROUTE = 'PATCH /v1/repositories/{repo_id}'
const PATCH_WHY = 'Except Code intelligence and the change trigger, these settings cannot be changed from this console yet: the controls show the served values.'

/** Where an absent `index_runs` / `used_by` would have been served (repo-index.md §6.1). */
const DETAIL_ROUTE = 'GET /v1/repositories/{repo_id}'

export function RepositoryDetail({
  repoId,
  tab,
  go,
  pr = null,
  graph = null,
}: {
  repoId: string
  tab: string | null
  go: (to: string) => void
  pr?: string | null
  /** The Graph view and symbol search the address opens on (a Test map row's link, QA G4-09). */
  graph?: GraphOpen | null
}) {
  const detail = useUrRead(() => loadRepository(repoId), `detail:${repoId}`)
  const index = useUrRead(() => loadRepositoryIndex(repoId), `index:${repoId}`)
  const d = detail.state.status === 'ok' || detail.state.status === 'stale' ? detail.state.data : null
  const title = d !== null ? repoName(d.repository) : repoId
  const crumb = (
    <UrCrumb trail={[{ label: 'Work' }, { label: 'Repositories', to: LIST }, { label: title }]} go={go} />
  )
  if (d === null) {
    return (
      <div className="ur-page ur-detail">
        {crumb}
        <PageHead title={title}>
          {/* The age is the registration's (V223): while it is pending or failed there is no page to date,
              and the index read beside it is not this page's read. Refresh still re-runs both. */}
          <UrRefresh reads={[detail, index]} ageOf={[detail]} />
        </PageHead>
        <UrRegion
          state={detail.state}
          route="GET /v1/repositories/{repo_id}"
          what="The repository's registration"
          onRetry={detail.reload}
          empty={
            <EmptyState kind="partial" heading="No registration to show">
              The API answered for {repoId} with nothing this page can read as a registration.
            </EmptyState>
          }
        >
          {() => (
            <EmptyState kind="partial" heading="No registration to show">
              The API answered for {repoId} with nothing this page can read as a registration.
            </EmptyState>
          )}
        </UrRegion>
      </div>
    )
  }
  return (
    <div className="ur-page ur-detail">
      {crumb}
      <DetailBody d={d} index={index} tab={tabOf(tab)} go={go} onRead={detail.reload} pr={pr} graph={graph} reads={[detail, index]} />
    </div>
  )
}

type IndexRead = ReturnType<typeof useUrRead<IndexDoc | null>>

function DetailBody({
  d,
  index,
  tab,
  go,
  onRead,
  pr,
  graph,
  reads,
}: {
  d: RepoDetailRead
  index: IndexRead
  tab: TabKey
  go: (to: string) => void
  onRead: () => void
  pr: string | null
  graph: GraphOpen | null
  /** The page's head reads, for its refresh and its one age (#98). */
  reads: readonly UrRead<unknown>[]
}) {
  const r = d.repository
  const f = freshness(r.index)
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<string | null>(null)
  // The run Index now or the full run asked for, waiting for its confirmation (RSA-15).
  const [confirm, setConfirm] = useState<'full' | 'incremental' | null>(null)
  const strip = useTabStrip(tab)
  // Read once for the page: Settings draws the table, and every tab is told when a language degraded the index (RSA-6).
  const langRoute = useUrRead(() => loadRepositoryLanguages(r.repo_id), `langs:${r.repo_id}`)
  const langs = languagesRead(langRoute.state, index.state)
  const doc = index.state.status === 'ok' || index.state.status === 'stale' ? index.state.data : null
  const cut = doc?.truncated.includes('test_map') === true

  async function indexNow(kind: 'full' | 'incremental') {
    setBusy(true)
    setRefused(null)
    const res = await runRepositoryIndex(r.repo_id, kind)
    setBusy(false)
    setConfirm(null)
    if (res.status === 'error') setRefused(writeFailure(res.error))
    else onRead()
  }

  const inFlight = r.index.in_flight_task_id
  // An array the detail did not serve is a dash with its reason, never "0".
  // Test map counts the edges it lists, as `index.coverage` serves them (QA G4-03),
  // with a "+" when the index cut its test map at the ceiling: 1,000 is where it stopped, not a total (RSA-4).
  const edges = r.index.coverage?.test_map_edges ?? null
  const counts: Partial<Record<TabKey, ReactNode>> = {
    ...(edges !== null ? { 'test-map': cut ? <span title="The index cut its test map at its size ceiling">{thousands(edges)}+</span> : thousands(edges) } : {}),
    'index-runs': d.index_runs === null ? <Dash why="Index runs are not served yet" /> : String(d.index_runs.length),
    'used-by': d.used_by === null ? <Dash why="Who used this index is not served yet" /> : String(d.used_by.length),
  }

  return (
    <>
      <PageHead title={repoName(r)}>
        <UrRefresh reads={reads} />
        <UrFreshPill f={f} />
        {inFlight !== null ? (
          <NamedMark mark="running" hue="live" word="indexing now" title={inFlight} />
        ) : (
          <>
            <Button size="sm" busy={busy} disabled={confirm !== null} onClick={() => setConfirm(r.index.current_sha === null ? 'full' : 'incremental')}>
              Index now
            </Button>
            {/* With no index yet, Index now is already a full run. This was Settings' "Re-run LSP pass": an
                enabled, mutating button on the tab headed "cannot be changed from this console", whose
                title alone said it queues a FULL run (QA G4-14). Its label says so now, beside Index now.
                Drawn as a button, not a ghost: as a ghost it read as a caption (RSA-15). */}
            {r.index.current_sha !== null && (
              <Button
                size="sm"
                busy={busy}
                disabled={confirm !== null}
                onClick={() => setConfirm('full')}
                title="Queues a full index run, which re-resolves every call site with the language servers (repo-index.md §3.5)"
              >
                Queue full index run (re-resolves with LSP)
              </Button>
            )}
          </>
        )}
      </PageHead>
      <MetaLine r={r} />
      {confirm !== null && inFlight === null && (
        <UrConfirmIndex
          name={repoName(r)}
          kind={confirm}
          base={r.index.current_sha}
          busy={busy}
          onConfirm={() => void indexNow(confirm)}
          onCancel={() => setConfirm(null)}
        />
      )}
      {refused !== null && (
        <Banner tone="bad" title="Index now was refused">
          {refused}
        </Banner>
      )}
      {(f.kind === 'behind' || f.kind === 'stale') && (
        <Banner tone={f.kind === 'stale' ? 'bad' : 'warn'} title={behindTitle(r)}>
          {f.kind === 'stale' ? `${f.why}. ` : ''}Planners using this index are told which files changed since it was built.
          {inFlight !== null ? ' An index run is in flight.' : ' No index run is in flight.'}
        </Banner>
      )}
      <DegradedLanguages state={langs.state} go={go} repoId={r.repo_id} here={tab === 'settings'} />
      {/* Its own org's GitHub permissions card (#1086): collapsed when all granted, open on a gap. */}
      <RepoPermissions repoId={r.repo_id} />
      <nav className="c-tabs ur-tabs" aria-label="Repository" ref={strip}>
        {TABS.map((t) => {
          const to = repoAddress(r.repo_id, t.key)
          return (
            <a
              key={t.key}
              href={addressToPath(to)}
              aria-current={t.key === tab ? 'page' : undefined}
              onClick={(e) => {
                if (!routedClick(e)) return
                e.preventDefault()
                go(to)
              }}
            >
              <span className="c-tab-label">{t.label}</span>
              {counts[t.key] !== undefined && <em>{counts[t.key]}</em>}
            </a>
          )
        })}
      </nav>
      {tab === 'overview' && <Overview d={d} index={index} go={go} cut={cut} />}
      {tab === 'graph' && <GraphTab r={r} open={graph} />}
      {tab === 'impact' && <ImpactTab r={r} pr={pr} />}
      {tab === 'test-map' && <TestMapTab r={r} index={index} go={go} cut={cut} />}
      {tab === 'hot-spots' && <HotSpotsTab r={r} index={index} />}
      {tab === 'index-runs' && (
        <Card title="Index runs">
          <RunList runs={d.index_runs} />
        </Card>
      )}
      {tab === 'settings' && (
        <SettingsTab r={r} langs={langs} langsRetry={langs.fromDoc ? index.reload : langRoute.reload} go={go} stops={d.hard_stops ?? null}
          context={d.context} onWrite={onRead} />
      )}
      {tab === 'used-by' && (
        <Card title="Used by">
          <UsedByList used={d.used_by} go={go} />
        </Card>
      )}
    </>
  )
}

/**
 * THE TAB STRIP SAYS THERE IS MORE OF IT (QA G4-19). At 390 the eight tabs
 * scroll and three were off the right edge with nothing to say so. The strip
 * now scrolls the open tab into view -- on itself, so the page never jumps --
 * and carries `data-fade` naming the edges with tabs behind them, which the
 * stylesheet fades.
 */
function useTabStrip(tab: TabKey) {
  const ref = useRef<HTMLElement | null>(null)
  useEffect(() => {
    const nav = ref.current
    if (nav === null) return
    const mark = () => nav.setAttribute('data-fade', stripFade(nav))
    const active = nav.querySelector<HTMLElement>('[aria-current="page"]')
    if (active !== null) {
      const box = nav.getBoundingClientRect()
      const at = active.getBoundingClientRect()
      const left = nav.scrollLeft
      const to = scrollToShow({ scrollLeft: left, clientWidth: nav.clientWidth }, { left: at.left - box.left + left, width: at.width })
      if (to !== null) nav.scrollLeft = to
    }
    mark()
    nav.addEventListener('scroll', mark, { passive: true })
    window.addEventListener('resize', mark)
    return () => {
      nav.removeEventListener('scroll', mark)
      window.removeEventListener('resize', mark)
    }
  }, [tab])
  return ref
}

function behindTitle(r: RepoRecord): string {
  const n = r.index.behind_by
  return n === null ? 'Behind the head.' : `Behind by ${n} commit${n === 1 ? '' : 's'}.`
}

function MetaLine({ r }: { r: RepoRecord }) {
  const ix = r.index
  return (
    <p className="ur-meta">
      <span>
        Default branch {r.default_branch === null ? <Dash why="The default branch was not served" /> : <b>{r.default_branch}</b>}
      </span>
      <span>
        index{' '}
        {ix.current_sha === null ? (
          <Dash why="No index has been built yet" />
        ) : (
          <b className="ur-sha" title={ix.current_sha}>
            {shortSha(ix.current_sha)}
          </b>
        )}
      </span>
      <span>
        head{' '}
        {ix.head_sha === null ? (
          <Dash why={HEAD_UNREAD} />
        ) : (
          <>
            <b className="ur-sha" title={ix.head_sha}>
              {shortSha(ix.head_sha)}
            </b>
            {ix.head_read_at !== null ? `, read ${timeAgo(ix.head_read_at)}` : ''}
          </>
        )}
      </span>
      <span>
        {r.allowed_profiles.length > 0 ? r.allowed_profiles.join(', ') : <Dash why="No runner profiles were served for this registration" />}
      </span>
      <span>
        registered by {r.created_by ?? <Dash why="Who registered it was not served" />}
        {r.created_at !== null ? `, ${r.created_at.slice(0, 10)}` : ''}
      </span>
    </p>
  )
}

// ---------------------------------------------------------------------------
// The index document's regions
// ---------------------------------------------------------------------------

const INDEX_ROUTE = 'GET /v1/repositories/{repo_id}/index'

/** The index document's region: not served, no index yet, failed, or the document. */
function IndexRegion({ r, index, children }: { r: RepoRecord; index: IndexRead; children: (doc: IndexDoc) => ReactNode }) {
  const noIndex = (
    <EmptyState kind="empty" heading="No index yet">
      {r.index.in_flight_task_id !== null
        ? 'The first index run is in flight; its figures appear here when it finishes.'
        : 'This repository has not been indexed. Index now queues a full run.'}
    </EmptyState>
  )
  // The API's own 404 (with its code) for a repository with no index is not a missing route.
  if (index.state.status === 'error' && index.state.error.httpStatus === 404 && !notServed(index.state.error)) return noIndex
  return (
    <UrRegion state={index.state} route={INDEX_ROUTE} what="The index document" onRetry={index.reload} empty={noIndex}>
      {(doc) => (doc === null ? noIndex : children(doc))}
    </UrRegion>
  )
}

function kib(bytes: number): string {
  return `${Math.round(bytes / 1024)}`
}

function Overview({ d, index, go, cut }: { d: RepoDetail; index: IndexRead; go: (to: string) => void; cut: boolean }) {
  const r = d.repository
  return (
    <>
      <IndexRegion r={r} index={index}>
        {(doc) => (
          <>
            <div className="ur-kv">
              <Tile label="Modules" value={String(doc.modules.length)} sub={moduleSub(doc)} />
              <Tile
                label="Entry points"
                value={String(doc.entry_points.length)}
                sub={doc.entry_points.length > 0 ? doc.entry_points.slice(0, 4).join(', ') : 'none found'}
              />
              <Tile
                label="Tests mapped"
                value={coverageWords(r.index.coverage)}
                why={testsMappedWhy(r)}
                sub={doc.unmapped.length > 0 ? `${pluralise(doc.unmapped.length, 'source path')} with no test edge` : (cutDetail(r.index.coverage, cut) ?? 'edges, not executed coverage')}
                warn={doc.unmapped.length > 0}
              />
              <Tile
                label="Index"
                value={doc.bytes === null ? null : kib(doc.bytes)}
                unit="KiB"
                why="The index's size was not served"
                sub={doc.truncated.length > 0 ? `truncated: ${doc.truncated.join(', ')}` : 'complete'}
              />
            </div>
            <div className="ur-cols">
              <Card title="Modules" action={<span className="ur-mu">{doc.modules.length} in the index</span>}>
                <ModuleRows doc={doc} />
              </Card>
              <Card title="Hot-spots" action={<span className="ur-mu">last 90 days on {r.default_branch ?? 'the default branch'}</span>}>
                <HotSpotRows doc={doc} limit={4} />
              </Card>
            </div>
          </>
        )}
      </IndexRegion>
      <div className="ur-cols">
        <Card
          title="Index runs"
          action={
            <UrLink to={repoAddress(r.repo_id, 'index-runs')} go={go} className="c-link is-card">
              {d.index_runs === null ? 'All' : `All ${d.index_runs.length}`} ›
            </UrLink>
          }
        >
          <RunList runs={d.index_runs?.slice(0, 4) ?? null} />
        </Card>
        <Card title="Schedule and change trigger">
          <ScheduleLines r={r} />
          <h3 className="ur-subh">Used by</h3>
          <UsedByList used={d.used_by?.slice(0, 4) ?? null} go={go} />
        </Card>
      </div>
    </>
  )
}

function moduleSub(doc: IndexDoc): string {
  const files = doc.modules.reduce<number | null>((n, m) => (n === null || m.files === null ? null : n + m.files), 0)
  const lines = doc.modules.reduce<number | null>((n, m) => (n === null || m.lines === null ? null : n + m.lines), 0)
  if (files === null || lines === null || doc.modules.length === 0) return 'files and lines not reported for every module'
  return `${files} files · ${lines >= 1000 ? `${Math.round(lines / 1000)}k` : lines} lines`
}

function Tile({ label, value, unit, sub, why, warn = false }: { label: string; value: string | null; unit?: string; sub: string; why?: string; warn?: boolean }) {
  return (
    <div className="ur-tile">
      <span className="ur-mu">{label}</span>{' '}
      <span className="ur-fig">
        {value === null ? (
          <Dash why={why ?? 'Not served'} />
        ) : (
          <>
            {value}
            {unit !== undefined && <small> {unit}</small>}
          </>
        )}
      </span>{' '}
      <span className={warn ? 'ur-sub is-warn' : 'ur-sub'}>{sub}</span>
    </div>
  )
}

function ModuleRows({ doc }: { doc: IndexDoc }) {
  if (doc.modules.length === 0) return <p className="ur-none">The index lists no modules.</p>
  return (
    <div className="ur-rows">
      {doc.modules.map((m) => (
        <div className="ur-mrow" key={m.path}>
          <code title={m.path}>
            <SlashBreaks path={m.path} />
          </code>
          <span>{m.purpose ?? <Dash why="The indexer wrote no purpose for this module" />}</span>
          <em>{m.files === null ? <Dash why="File count not reported" /> : m.files}</em>
        </div>
      ))}
    </div>
  )
}

/**
 * What the index's history depth means for co-change (QA G4-06): a dash with
 * the API's reason when it was impossible -- a one-commit-deep clone -- and
 * "a lower bound" when the history stops inside the window. Nothing when the
 * window was read whole, or the API served no judgement.
 */
function HistoryLine({ doc, what }: { doc: IndexDoc; what: string }) {
  const h = doc.history
  if (h === null || (h.co_change !== 'impossible' && h.co_change !== 'partial')) return null
  if (h.co_change === 'partial') {
    return <p className="ur-hint is-warn ur-history">{h.reason ?? 'The index read only part of the window: counts are a lower bound.'}</p>
  }
  return (
    <p className="ur-hint ur-history">
      {what}: <Dash why={h.reason ?? 'The indexer read no history inside the window'} /> not known for this index.
    </p>
  )
}

function HotSpotRows({ doc, limit }: { doc: IndexDoc; limit?: number }) {
  const rows = limit === undefined ? doc.hot_spots : doc.hot_spots.slice(0, limit)
  const max = rows.reduce((n, h) => Math.max(n, h.changes ?? 0), 0)
  const co = doc.co_changes[0]
  const impossible = doc.history?.co_change === 'impossible'
  return (
    <>
      <HistoryLine doc={doc} what="Hot-spots and co-change" />
      {rows.length === 0 ? (
        impossible ? null : (
        <p className="ur-none">The index lists no hot-spots.</p>
        )
      ) : (
        <div className="ur-rows">
          {rows.map((h) => (
            <div className="ur-hs" key={h.path}>
              {/* Ellipsised in a narrow card: the whole path is its title, and on a phone it wraps on its own line (V218). */}
              <span className="ur-p" title={h.path}>
                <SlashBreaks path={h.path} />
              </span>
              <span className="ur-bar is-warn" role="img" aria-label={`${h.changes ?? 'unknown'} changes`}>
                {h.changes !== null && max > 0 && <i style={{ width: `${Math.round((100 * h.changes) / max)}%` }} />}
              </span>
              <em>{h.changes === null ? <Dash why="Change count not reported" /> : h.changes}</em>
            </div>
          ))}
        </div>
      )}
      {co !== undefined && (
        <p className="ur-cochange">
          Changed together most: <code>{co.a}</code> ↔ <code>{co.b}</code>
          {co.times !== null ? ` (${co.times} times).` : '.'}
        </p>
      )}
    </>
  )
}

function TestMapTab({ r, index, go, cut }: { r: RepoRecord; index: IndexRead; go: (to: string) => void; cut: boolean }) {
  const [filter, setFilter] = useState('')
  const doc = index.state.status === 'ok' || index.state.status === 'stale' ? index.state.data : null
  return (
    <>
      <PathLookup r={r} doc={doc} />
      <Card title="Test map">
        <IndexRegion r={r} index={index}>
          {(doc) => (
            <div className="ur-tmap">
              <TestsMapped r={r} cut={cut} />
              <p className="ur-hint">Edges from the index: a source glob and the tests that reach it, each with its evidence. Not executed coverage.</p>
              <HistoryLine doc={doc} what="Co-change evidence" />
              {doc.truncated.includes('test_map') && (
                <p className="ur-hint is-warn">The index&apos;s test map was cut at its size ceiling: these are the edges it kept.</p>
              )}
              <input type="search" className="ur-search" aria-label="Filter by path" placeholder="Filter by path" value={filter} onChange={(e) => setFilter(e.target.value)} />
              <EdgeGroups r={r} doc={doc} filter={filter} go={go} />
              {doc.unmapped.length > 0 && (
                <>
                  <h3 className="ur-subh">No test edge</h3>
                  <div className="ur-unmapped">
                    {doc.unmapped.map((p) => (
                      <code key={p}>{p}</code>
                    ))}
                  </div>
                </>
              )}
              <AlwaysRun rows={doc.always_tests} />
              <Suites doc={doc} />
            </div>
          )}
        </IndexRegion>
      </Card>
    </>
  )
}

function HotSpotsTab({ r, index }: { r: RepoRecord; index: IndexRead }) {
  return (
    <Card title="Hot-spots" action={<span className="ur-mu">last 90 days on {r.default_branch ?? 'the default branch'}</span>}>
      <IndexRegion r={r} index={index}>
        {(doc) => <HotSpotRows doc={doc} />}
      </IndexRegion>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Index runs, schedule, used by
// ---------------------------------------------------------------------------

function duration(run: IndexRun): ReactNode {
  const done = run.state !== null && TERMINAL_STATES.has(run.state)
  if (!done) return <Dash why="Still running" />
  const from = run.started_at ?? run.queued_at
  if (from === null || run.ended_at === null) return <Dash why="Start or end time not served" />
  return formatDuration(new Date(run.ended_at).getTime() - new Date(from).getTime())
}

function RunList({ runs }: { runs: readonly IndexRun[] | null }) {
  if (runs === null) return <UrNotServed route={`${DETAIL_ROUTE} index_runs`} what="Index runs" plural />
  if (runs.length === 0) return <p className="ur-none ur-runs">No index runs yet.</p>
  return (
    <div className="ur-runs ur-rows">
      {runs.map((run) => (
        <div className="ur-run" key={run.task_id}>
          {run.state === null ? (
            <NamedMark mark={null} hue="unknown" word="state not read" bare />
          ) : (
            <StatePill state={run.state} form="mark" />
          )}
          {run.commit_sha === null ? <Dash why="Commit not served" /> : <em className="ur-sha" title={run.commit_sha}>{shortSha(run.commit_sha)}</em>}
          <span>{run.kind ?? <Dash why="Kind not served" />}</span>
          <small>
            {[run.trigger, run.started_at !== null ? `started ${timeAgo(run.started_at)}` : run.queued_at !== null ? `queued ${timeAgo(run.queued_at)}` : null, run.end_cause]
              .filter((x): x is string => x !== null)
              .join(' · ')}
          </small>
          <em>{duration(run)}</em>
        </div>
      ))}
    </div>
  )
}

function ScheduleLines({ r }: { r: RepoRecord }) {
  const ix = r.index
  const every = intervalWords(ix.interval_hours)
  const branch = r.default_branch ?? 'the default branch'
  return (
    <div className="ur-sched">
      <span>
        Re-index{' '}
        {ix.interval_hours === null ? (
          <Dash why="The interval was not served" />
        ) : every === null ? (
          <b>off</b>
        ) : (
          <>
            <b>every {every}</b>
            {ix.next_run_at !== null ? ` · next ${timeAgo(ix.next_run_at).includes('ago') ? 'due now' : `at ${new Date(ix.next_run_at).toTimeString().slice(0, 5)}`}` : ''}
          </>
        )}
      </span>
      <span>
        Change trigger{' '}
        {ix.on_change === null ? (
          <Dash why="The change trigger was not served" />
        ) : ix.on_change === 'webhook' ? (
          <b>webhook on {branch}</b>
        ) : ix.on_change === 'poll' ? (
          <>
            <b>poll {branch}</b>
            {ix.min_change_interval_minutes !== null ? `, at most one run per ${ix.min_change_interval_minutes} min` : ''}
          </>
        ) : (
          <b>off</b>
        )}
      </span>
      <span>
        Full run{' '}
        {ix.full_every_days === null ? <Dash why="The full-run cadence was not served" /> : <b>every {ix.full_every_days} days</b>}
      </span>
      <span>Paused {ix.paused === null ? <Dash why="Whether it is paused was not served" /> : <b>{ix.paused ? 'yes' : 'no'}</b>}</span>
    </div>
  )
}

function usedAddress(u: UsedBy): string {
  if (u.kind === 'workflow') return `work/workflows?${new URLSearchParams({ wf: u.id }).toString()}`
  if (u.kind === 'task') return `work/task/${encodeURIComponent(u.id)}`
  return `work/runs?${new URLSearchParams({ run: u.id }).toString()}`
}

function UsedByList({ used, go }: { used: readonly UsedBy[] | null; go: (to: string) => void }) {
  if (used === null) return <UrNotServed route={`${DETAIL_ROUTE} used_by`} what="The runs and workflows that used this index" plural />
  if (used.length === 0) return <p className="ur-none ur-used">No run or workflow has used this index yet.</p>
  const noun = { run: 'Run', workflow: 'Workflow', task: 'Agent' } as const
  return (
    <div className="ur-used ur-rows">
      {used.map((u) => (
        <div key={`${u.kind}:${u.id}`}>
          <ToneMark tone={u.state === 'DONE' || u.state === 'SUCCEEDED' ? 'ok' : u.state === 'FAILED' ? 'bad' : u.state === null ? 'unknown' : 'live'} />
          <span>
            {noun[u.kind]}{' '}
            <UrLink to={usedAddress(u)} go={go}>
              <b>{u.title ?? u.id}</b>
            </UrLink>
            {u.role !== null ? ` · ${u.role}` : ''}
          </span>
          {u.index_sha === null ? <Dash why="Which index version it used was not served" /> : <small className="ur-sha">{shortSha(u.index_sha)}</small>}
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Settings A
// ---------------------------------------------------------------------------

const LANG_TONE: Readonly<Record<string, string>> = { ok: 'ok', partial: 'warn', unsupported: 'warn', failing: 'bad', timed_out: 'bad' }

/**
 * The languages table: the languages route's rows, or -- when this API does
 * not serve that route -- the index document's own `languages` (QA G4-13).
 * "Not served yet" only when neither has them.
 */
type LanguagesRead = { state: Result<LanguageRow[]>; fromDoc: boolean }

function languagesRead(route: Result<LanguageRow[]>, index: Result<IndexDoc | null>): LanguagesRead {
  if (route.status !== 'error' || !notServed(route.error)) return { state: route, fromDoc: false }
  if (index.status === 'loading') return { state: index, fromDoc: false }
  if (index.status !== 'ok' && index.status !== 'stale') return { state: route, fromDoc: false }
  const doc = index.data
  if (doc === null || doc.languages === null) return { state: route, fromDoc: false }
  if (doc.languages.length === 0) return { state: { status: 'empty', fetchedAt: index.fetchedAt }, fromDoc: true }
  return { state: { ...index, data: doc.languages }, fromDoc: true }
}

/**
 * A language whose language-server pass did not finish: the index is degraded for its files (RSA-6). `partial`
 * finished some of them before its budget ran out, and those keep their edges (#1693).
 */
const DEGRADED: Readonly<Record<string, string>> = { partial: 'partly covered', timed_out: 'timed out', failing: 'failed' }

/** "python (1,046 files)": a degraded language, with its size when served. */
function langWords(l: LanguageRow): string {
  return l.files === null ? l.language : `${l.language} (${thousands(l.files)} file${l.files === 1 ? '' : 's'})`
}

/** "python: covered 702 of 1,447 files": a partial language, with how far its server got when served. */
function partialWords(l: LanguageRow): string | null {
  return l.files_covered === null || l.files === null ? null : `${l.language}: covered ${thousands(l.files_covered)} of ${thousands(l.files)} files`
}

/**
 * A DEGRADED INDEX IS SAID ON EVERY TAB, NOT ONLY IN SETTINGS (live QA RSA-6,
 * 2026-10-11). When a language's server pass timed out or failed, the call
 * sites in its files are not resolved, so Impact and test selection answer
 * wider for them -- on SwarmCloud, "the full suite". Only the Settings table
 * said so, and nothing on the tab where the fallback showed explained it.
 * Nothing is drawn while the languages are unread or every pass finished.
 */
function DegradedLanguages({ state, go, repoId, here }: { state: Result<LanguageRow[]>; go: (to: string) => void; repoId: string; here: boolean }) {
  if (state.status !== 'ok' && state.status !== 'stale') return null
  const rows = state.data.filter((l) => l.status !== null && DEGRADED[l.status] !== undefined)
  if (rows.length === 0) return null
  // A partial language says how far its server got; one whose counts were not served falls into its word group.
  const covered = (l: LanguageRow): string | null => (l.status === 'partial' ? partialWords(l) : null)
  const groups = [
    ...rows.flatMap((l) => {
      const words = covered(l)
      return words === null ? [] : [words]
    }),
    ...Object.entries(DEGRADED)
      .map(([status, word]) => ({ word, names: rows.filter((l) => l.status === status && covered(l) === null).map(langWords) }))
      .filter((g) => g.names.length > 0)
      .map((g) => `${g.names.join(' and ')} ${g.word}`),
  ]
  const partly = rows.some((l) => l.status === 'partial')
  return (
    <div className="ur-degraded" data-degraded={rows.map((l) => l.language).join(' ')}>
      <Banner tone="warn" title="This index is degraded" role="status">
        The language-server pass for {groups.join('; ')}, so call sites in {rows.length === 1 ? 'its' : 'their'} files
        {partly ? ' the server did not cover' : ''} are not resolved. Impact and test selection answer wider for those files, up to the full suite.
        {!here && (
          <>
            {' '}
            <UrLink to={repoAddress(repoId, 'settings')} go={go}>
              Languages in Settings ›
            </UrLink>
          </>
        )}
      </Banner>
    </div>
  )
}

/** Who sets the policy, as a chip; none when the policy itself was not served beside a "set here" (QA G4-15). */
function policyChip(pol: RepoRecord['selection_policy']): string | null {
  if (pol.inherited_from_tenant === true) return 'from the tenant default'
  if (pol.inherited_from_tenant === false && pol.policy !== null) return 'set for this repository'
  return null
}

function SettingsTab({
  r,
  langs,
  langsRetry,
  go,
  stops,
  context,
  onWrite,
}: {
  r: RepoRecord
  langs: LanguagesRead
  langsRetry: () => void
  go: (to: string) => void
  stops: RepoHardStops | null
  context: RepoContext
  onWrite: () => void
}) {
  const resolved = useUrRead(() => loadResolvedToken(r.repo_id), `token:${r.repo_id}`)
  const pol = r.selection_policy
  const policy = pol.inherited_from_tenant === true ? 'inherit' : pol.policy
  const chip = policyChip(pol)
  return (
    <>
      <p className="ur-hint ur-locked" data-route={PATCH_ROUTE} title={`Not served: ${PATCH_ROUTE}`}>
        {PATCH_WHY}
      </p>
      <div className="ur-cols">
        <div className="ur-stack">
          <Card title="Schedule and change trigger">
            <ScheduleLines r={r} />
            <ChangeTriggerControl repoId={r.repo_id} served={r.index.on_change} onWrite={onWrite} />
          </Card>
          {/* Drawn only where the registration serves the fields: an API older than lane S11 has no card to show. */}
          {stops !== null && <HardStopsCard stops={stops} />}
        </div>
        <div className="ur-stack">
          <Card title="Graph" className="ur-graph">
            <div className="ur-fgrid">
              <span className="ur-fl">
                Graph depth<small>callers walked by impact</small>
              </span>
              <span className="ur-slider">
                {r.graph.depth === null ? (
                  <Dash why="The graph depth was not served" />
                ) : (
                  <>
                    <input type="range" min={1} max={6} value={r.graph.depth} disabled aria-label="Graph depth" readOnly />
                    <span>
                      <b>{r.graph.depth}</b> of 1-6
                    </span>
                  </>
                )}
              </span>
              <span className="ur-fl">
                Minimum confidence<small>paths below are cut</small>
              </span>
              <span className="ur-slider">
                {r.graph.min_confidence === null ? (
                  <Dash why="The minimum confidence was not served" />
                ) : (
                  <>
                    <input type="range" min={0} max={1} step={0.05} value={r.graph.min_confidence} disabled aria-label="Minimum confidence" readOnly />
                    <b>{r.graph.min_confidence}</b>
                  </>
                )}
              </span>
            </div>
          </Card>
          <CodeIntelligenceCard repoId={r.repo_id} served={context} onWrite={onWrite} go={go} />
          <Card
            title="Selection policy"
            className="ur-policy"
            action={chip === null ? undefined : <Chip>{chip}</Chip>}
          >
            <UrRadio
              label="Selection policy"
              disabled
              value={policy}
              options={[
                { key: 'inherit', label: 'Inherit' },
                { key: 'P1', label: 'P1 selected gate' },
                { key: 'P2', label: 'P2 fast gate' },
                { key: 'P3', label: 'P3 gate with fallback' },
                { key: 'off', label: 'Off' },
              ]}
            />
            <UrRadio
              label="Execution mode"
              disabled
              value={pol.mode}
              options={[
                { key: 'X2', label: "X2 run in this repository's GitHub Actions" },
                { key: 'X1', label: 'X1 run in SwarmCloud' },
              ]}
            />
            <p className="ur-hint">{rulesetHint(pol.policy, r.default_branch, pol.inherited_from_tenant === true)}</p>
          </Card>
          <Card title="Resolved token" className="ur-resolved" action={<UrNavButton to={GT_PAGE} go={go} size="sm">Git tokens</UrNavButton>}>
            <UrRegion state={resolved.state} route="GET /v1/git-tokens" what="The token that resolves here" onRetry={resolved.reload}>
              {(t) => <ResolvedRows t={t} />}
            </UrRegion>
          </Card>
        </div>
      </div>
      {/* The full row, not half of it (V121): five columns in a half-width card
          broke grammar names at their hyphens and wrapped the head. */}
      <Card title="Languages detected" className="ur-langs">
        {langs.fromDoc && <p className="ur-hint ur-lang-src">Read from the index document: the languages route is not served by this API.</p>}
        <UrRegion
          state={langs.state}
          route="GET /v1/repositories/{repo_id}/languages"
          what="The languages table"
          onRetry={langsRetry}
          empty={<p className="ur-none">The index detected no languages.</p>}
        >
          {(rows) => (
            <div className="ur-rows">
              <div className="ur-lang-h" aria-hidden>
                <span>language</span>
                <span>grammar</span>
                <span>language server</span>
                <span>last index</span>
                <span>status</span>
              </div>
              {rows.map((l) => (
                // Each cell names its column (V221): stacked on a phone the head row is gone, and
                // "pyright", "410 files" and a dash read as nothing without it.
                <div className="ur-lang" key={l.language}>
                  <b>{l.language}</b>
                  <span data-label="grammar">{l.grammar === null ? <Dash why="No tree-sitter grammar is bundled for this language" /> : <code>{l.grammar}</code>}</span>
                  <span data-label="language server">{l.server === null ? <Dash why="No language server is supported for this language" /> : <code>{l.server}</code>}</span>
                  <small data-label="last index">{langNote(l)}</small>
                  <span className="ur-lang-st">
                    <ToneMark tone={LANG_TONE[l.status ?? ''] ?? 'unknown'}>{l.status === null ? 'not read' : l.status.replace(/_/g, ' ')}</ToneMark>
                  </span>
                </div>
              ))}
            </div>
          )}
        </UrRegion>
      </Card>
    </>
  )
}

/**
 * CODE INTELLIGENCE, THIS REPOSITORY'S OVERRIDE (CTXQ-UI;
 * docs/design/context-quality.md §4.7). Whether a step on this repository is
 * staged with its graph context -- the local pack and the graph tools, which
 * send nothing off the platform. `inherit` follows the workspace's Code
 * intelligence setting; on and off override it. The one control on this tab
 * that writes (PATCH_WHY is about the others): `inherit` is sent as itself,
 * because that is what clears the override, as `merge_policy` does. Whether
 * Jev is ever sent code is NOT here -- it is the workspace's switch, on
 * Settings › Workspace, and a repository cannot turn it on for itself.
 */
const CONTEXT_OPTIONS: readonly { key: RepoContext; label: string }[] = [
  { key: 'inherit', label: 'Inherit' },
  { key: 'on', label: 'On' },
  { key: 'off', label: 'Off' },
]

function CodeIntelligenceCard({ repoId, served, onWrite, go }: { repoId: string; served: RepoContext; onWrite: () => void; go: (to: string) => void }) {
  // What the control shows: the served value, or the one being saved.
  const [picked, setPicked] = useState<RepoContext>(served)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  useEffect(() => {
    if (!busy) setPicked(served)
  }, [served, busy])

  async function choose(next: RepoContext) {
    if (busy || next === picked) return
    const was = picked
    setPicked(next)
    setBusy(true)
    setFailed(null)
    const res = await patchRepositoryContext(repoId, next)
    setBusy(false)
    if (res.status === 'error') {
      setPicked(was)
      setFailed(`Code intelligence was not changed: ${writeFailure(res.error)}`)
      return
    }
    onWrite()
  }

  return (
    <Card title="Code intelligence" className="ur-codeintel">
      <UrRadio label="Code intelligence" value={picked} options={CONTEXT_OPTIONS} disabled={busy} onChange={(k) => void choose(k)} />
      <p className="ur-hint">
        {picked === 'inherit' ? "Follows this workspace's setting. " : picked === 'on' ? 'On for this repository. ' : 'Off for this repository. '}
        Steps here get the repository&rsquo;s graph context: a ranked pack and the graph tools, built from its index.
        Nothing leaves the platform. Whether Jev is ever sent code is the workspace&rsquo;s switch, on{' '}
        <UrLink to={WORKSPACE_SETTINGS} go={go}>
          Settings › Workspace
        </UrLink>
        .
      </p>
      {failed !== null && <Banner tone="bad" title={failed} role="alert" />}
    </Card>
  )
}

/**
 * THE CHANGE TRIGGER, CHANGED IN PLACE (HOOKS-UI, part of #1384). One PATCH
 * carrying `index.on_change` alone. Webhook is always offered: it is "poll AND
 * push" (TRIGGER_OPTIONS), so a registration that chose it while the push
 * receiver is off is still read by the five-minute poll. Any member may write
 * it (`IndexSettings` passes no admin check), so no role hides it here.
 */
function ChangeTriggerControl({ repoId, served, onWrite }: { repoId: string; served: ChangeTrigger | null; onWrite: () => void }) {
  const [picked, setPicked] = useState<ChangeTrigger | null>(served)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  useEffect(() => {
    if (!busy) setPicked(served)
  }, [served, busy])

  async function choose(next: ChangeTrigger) {
    if (busy || next === picked) return
    const was = picked
    setPicked(next)
    setBusy(true)
    setFailed(null)
    const res = await patchRepositoryTrigger(repoId, next)
    setBusy(false)
    if (res.status === 'error') {
      setPicked(was)
      setFailed(`The change trigger was not changed: ${writeFailure(res.error)}`)
      return
    }
    onWrite()
  }

  return (
    <div className="ur-trigger">
      <UrRadio<ChangeTrigger> label="Change trigger" value={picked} options={TRIGGER_OPTIONS} disabled={busy} onChange={(k) => void choose(k)} />
      <p className="ur-hint">{WEBHOOK_NOTE}</p>
      {failed !== null && <Banner tone="bad" title={failed} role="alert" />}
    </div>
  )
}

/** Settings › Workspace, where the workspace's Code intelligence switch is. */
const WORKSPACE_SETTINGS = 'settings/workspace'

/**
 * The hard stops' two registration fields (docs/schedules.md §4.4). Shown,
 * not changed, like every card here: `platform` is a platform admin's to set
 * and clear (audited in admin_audit), and the floor paths are marked so no
 * one reads them as removable.
 */
function HardStopsCard({ stops }: { stops: RepoHardStops }) {
  const floor = new Set(stops.floor)
  return (
    <Card
      title="Hard stops"
      className="ur-hardstops"
      action={stops.platform ? <Chip>platform repository</Chip> : undefined}
    >
      <p className="ur-meta" data-platform={stops.platform ? 'true' : 'false'}>
        <span>
          Platform repository <b>{stops.platform ? 'yes' : 'no'}</b>
        </span>
        <span>set and cleared by a platform admin only</span>
      </p>
      <p className="ur-hint">
        {stops.platform
          ? "A change to a protected path here is held for the platform owner's approval."
          : 'A change to a protected path here is held for a second member of the tenant.'}
      </p>
      <h3 className="ur-subh">Protected paths</h3>
      <div className="ur-rows">
        {stops.hard_stop_paths.map((path) => (
          <div className="ur-hardstop" key={path} data-floor={floor.has(path) ? 'true' : 'false'}>
            <code>{path}</code>
            {floor.has(path) && (
              <span className="ur-mu" title="One of the four defaults: a member can add paths but cannot remove this one">
                {' '}default, cannot be removed
              </span>
            )}
          </div>
        ))}
      </div>
    </Card>
  )
}

function langNote(l: { files: number | null; resolved: number | null; fallback: string | null }): ReactNode {
  const parts = [l.files !== null ? `${l.files} files` : null, l.resolved !== null ? `${pct(l.resolved)} resolved` : null].filter((x): x is string => x !== null)
  if (l.fallback !== null) parts.push(l.fallback)
  return parts.length === 0 ? <Dash why="No file count or resolution rate served" /> : parts.join(' · ')
}

/** What the picked policy expects of the branch rules (repo-index.md §4.4). The live ruleset is not read here. */
function rulesetHint(policy: string | null, branch: string | null, inherited: boolean): string {
  const on = branch ?? 'the default branch'
  if (policy === 'P1' || policy === 'P3') {
    return `The ruleset this policy expects on ${on}: swarmcloud/selected-tests required; full-suite jobs not required${policy === 'P3' ? ' (they still run when a change falls back to the full suite)' : ''}.`
  }
  if (policy === 'P2') return `The ruleset this policy expects on ${on}: the full suite required; swarmcloud/selected-tests reports first.`
  if (policy === 'off') return 'Selection is off: every pull request runs the full suite.'
  // The registration says it inherits and carries no policy of its own (repositories.py serves exactly that).
  if (inherited) return "Inherited from the tenant default, which this page does not read: the expected ruleset is the tenant's."
  return 'The policy was not served.'
}

function ResolvedRows({ t }: { t: ResolvedToken }) {
  const now = Date.now()
  if (t.token === null) {
    return (
      <EmptyState kind="partial" heading="No token resolves for this repository">
        {t.reason ?? 'Neither a repository token nor the tenant token covers it.'}
      </EmptyState>
    )
  }
  const k = t.token
  const exp = k.expires_at === null ? null : expiryWords(k.expires_at, now)
  return (
    <>
      <p className="ur-meta">
        <span>
          <b>{k.scope} token</b> {k.forge_login ?? <Dash why="The account the token acts as was not read" />}
        </span>
        <span>
          {kindWords(k.kind) ?? 'kind not read'}
          {k.last4 !== null ? ` ··· ${k.last4}` : ''}
        </span>
        <span>
          {exp === null ? (
            <>
              expires <Dash why="No expiry recorded" />
            </>
          ) : exp.startsWith('expired') ? (
            <b className="ur-bad">{exp}</b>
          ) : (
            <>expires {exp}</>
          )}
        </span>
        <span>last verified {k.verified_at === null ? <Dash why="Never verified" /> : timeAgo(k.verified_at)}</span>
      </p>
      <p className="ur-meta">
        <span>{t.order === 'R2' ? 'order R2: repository token, then tenant token' : t.order === null ? <>order <Dash why="The resolution order was not served" /></> : `order ${t.order}`}</span>
        {t.user_token !== null && (
          <span>your user token{t.user_token.last4 !== null ? ` (…${t.user_token.last4})` : ''} is used only for attribution</span>
        )}
      </p>
      {t.capabilities === null ? (
        <UrNotServed route="GET /v1/git-tokens" what="The token's capability row" />
      ) : (
        <UrCapRow row={t.capabilities} />
      )}
    </>
  )
}
