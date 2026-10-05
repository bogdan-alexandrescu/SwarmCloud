import { useState, type ReactNode } from 'react'
import { loadRepository, loadRepositoryIndex, loadRepositoryLanguages, loadResolvedToken, runRepositoryIndex } from './api'
import { Banner, Button, Card, Chip, Dash, EmptyState, NamedMark, StatePill, ToneMark, routedClick } from './components'
import { addressToPath } from './paths'
import {
  freshness, HEAD_UNREAD, intervalWords, kindWords, pct, repoName, shortSha, notServed, expiryWords,
  type IndexDoc, type IndexRun, type RepoDetail, type RepoRecord, type ResolvedToken, type UsedBy,
} from './RepositoriesData'
import {
  LIST, GT_PAGE, TestsMapped, UrCapRow, UrCrumb, UrFreshPill, UrLink, UrNavButton, UrNotServed, UrRadio, UrRegion, repoAddress,
  useUrRead, writeFailure,
} from './RepositoriesParts'
import { PageHead } from './Shell'
import { formatDuration, timeAgo, TERMINAL_STATES } from './types'

/**
 * ONE REPOSITORY, pick A (repositories.html screen 4): tabs Overview, Test
 * map, Hot-spots, Index runs, Settings, Used by -- one tab per part of the
 * index -- with Settings drawn as screen 11's pick A: cards for the schedule,
 * the languages, the graph, the selection policy (P3 / X2, PICKS.md) and the
 * git token that resolves here with its capability row.
 *
 * EVERY FIGURE NAMES THE COMMIT IT DESCRIBES: the meta line carries the index
 * sha and the head sha with when the head was read, and an index behind its
 * head leads with a banner (states.html C, page tier) before anything else.
 *
 * EACH REGION READS ITS OWN ROUTE, so a route not built yet costs only its
 * region: the index document, the languages and the resolved token each say
 * "not served yet" in place, naming their route, while the rest draws.
 *
 * SETTINGS SHOW THE SERVED VALUES AND DO NOT CHANGE THEM. Changing one is
 * `PATCH /v1/repositories/{repo_id}` (repo-index.md §6.1), and fetch.ts's
 * `write()` sends POST, PUT and DELETE only; the controls are drawn as the
 * frame draws them, disabled, with that reason beside them.
 */

const TABS = [
  { key: 'overview', label: 'Overview' },
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
const PATCH_WHY = 'These settings cannot be changed from this console yet: the controls show the served values.'

/** Where an absent `index_runs` / `used_by` would have been served (repo-index.md §6.1). */
const DETAIL_ROUTE = 'GET /v1/repositories/{repo_id}'

export function RepositoryDetail({ repoId, tab, go }: { repoId: string; tab: string | null; go: (to: string) => void }) {
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
        <PageHead title={title}>{null}</PageHead>
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
      <DetailBody d={d} index={index} tab={tabOf(tab)} go={go} onRead={detail.reload} />
    </div>
  )
}

type IndexRead = ReturnType<typeof useUrRead<IndexDoc | null>>

function DetailBody({ d, index, tab, go, onRead }: { d: RepoDetail; index: IndexRead; tab: TabKey; go: (to: string) => void; onRead: () => void }) {
  const r = d.repository
  const f = freshness(r.index)
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<string | null>(null)

  async function indexNow(kind: 'full' | 'incremental') {
    setBusy(true)
    setRefused(null)
    const res = await runRepositoryIndex(r.repo_id, kind)
    setBusy(false)
    if (res.status === 'error') setRefused(writeFailure(res.error))
    else onRead()
  }

  const inFlight = r.index.in_flight_task_id
  // An array the detail did not serve is a dash with its reason, never "0".
  const counts: Partial<Record<TabKey, ReactNode>> = {
    ...(r.index.coverage !== null ? { 'test-map': pct(r.index.coverage) } : {}),
    'index-runs': d.index_runs === null ? <Dash why="Index runs are not served yet" /> : String(d.index_runs.length),
    'used-by': d.used_by === null ? <Dash why="Who used this index is not served yet" /> : String(d.used_by.length),
  }

  return (
    <>
      <PageHead title={repoName(r)} action={<UrFreshPill f={f} />}>
        {inFlight !== null ? (
          <NamedMark mark="running" hue="live" word="indexing now" title={inFlight} />
        ) : (
          <Button size="sm" busy={busy} onClick={() => void indexNow(r.index.current_sha === null ? 'full' : 'incremental')}>
            Index now
          </Button>
        )}
      </PageHead>
      <MetaLine r={r} />
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
      <nav className="c-tabs ur-tabs" aria-label="Repository">
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
      {tab === 'overview' && <Overview d={d} index={index} go={go} />}
      {tab === 'test-map' && <TestMapTab r={r} index={index} />}
      {tab === 'hot-spots' && <HotSpotsTab r={r} index={index} />}
      {tab === 'index-runs' && (
        <Card title="Index runs">
          <RunList runs={d.index_runs} />
        </Card>
      )}
      {tab === 'settings' && <SettingsTab r={r} go={go} busy={busy} onFull={() => void indexNow('full')} />}
      {tab === 'used-by' && (
        <Card title="Used by">
          <UsedByList used={d.used_by} go={go} />
        </Card>
      )}
    </>
  )
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

function Overview({ d, index, go }: { d: RepoDetail; index: IndexRead; go: (to: string) => void }) {
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
                value={r.index.coverage === null ? null : pct(r.index.coverage)}
                why="The index did not report its test-map coverage"
                sub={doc.unmapped.length > 0 ? `${doc.unmapped.length} source paths with no test edge` : 'edges, not executed coverage'}
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
          <code>{m.path}</code>
          <span>{m.purpose ?? <Dash why="The indexer wrote no purpose for this module" />}</span>
          <em>{m.files === null ? <Dash why="File count not reported" /> : m.files}</em>
        </div>
      ))}
    </div>
  )
}

function HotSpotRows({ doc, limit }: { doc: IndexDoc; limit?: number }) {
  const rows = limit === undefined ? doc.hot_spots : doc.hot_spots.slice(0, limit)
  const max = rows.reduce((n, h) => Math.max(n, h.changes ?? 0), 0)
  const co = doc.co_changes[0]
  return (
    <>
      {rows.length === 0 ? (
        <p className="ur-none">The index lists no hot-spots.</p>
      ) : (
        <div className="ur-rows">
          {rows.map((h) => (
            <div className="ur-hs" key={h.path}>
              <span className="ur-p">{h.path}</span>
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

function TestMapTab({ r, index }: { r: RepoRecord; index: IndexRead }) {
  const [filter, setFilter] = useState('')
  return (
    <Card title="Test map">
      <IndexRegion r={r} index={index}>
        {(doc) => {
          const f = filter.trim().toLowerCase()
          const rows = doc.test_map.filter((t) => f === '' || t.source.toLowerCase().includes(f) || t.tests.some((x) => x.toLowerCase().includes(f)))
          return (
            <div className="ur-tmap">
              <TestsMapped r={r} />
              <p className="ur-hint">Edges from the index: a source path and the tests that reach it, with the evidence. Not executed coverage.</p>
              <input type="search" className="ur-search" aria-label="Filter by path" placeholder="Filter by path" value={filter} onChange={(e) => setFilter(e.target.value)} />
              <div className="ur-rows">
                {rows.map((t) => (
                  <div className="ur-tmap-row" key={t.source}>
                    <code>{t.source}</code>
                    <span className="ur-tests">
                      {t.tests.length === 0 ? <Dash why="No tests listed for this path" /> : t.tests.map((x) => <code key={x}>{x}</code>)}
                    </span>
                    {t.evidence === null ? <Dash why="No evidence recorded" /> : <Chip>{t.evidence}</Chip>}
                  </div>
                ))}
                {rows.length === 0 && <p className="ur-none">{doc.test_map.length === 0 ? 'The index maps no tests.' : 'No path matches the filter.'}</p>}
              </div>
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
            </div>
          )
        }}
      </IndexRegion>
    </Card>
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
  if (runs === null) return <UrNotServed route={`${DETAIL_ROUTE} index_runs`} what="The repository's index runs" />
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
  if (used === null) return <UrNotServed route={`${DETAIL_ROUTE} used_by`} what="Which runs and workflows used this index" />
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

const LANG_TONE: Readonly<Record<string, string>> = { ok: 'ok', unsupported: 'warn', failing: 'bad', timed_out: 'bad' }

function SettingsTab({ r, go, busy, onFull }: { r: RepoRecord; go: (to: string) => void; busy: boolean; onFull: () => void }) {
  const langs = useUrRead(() => loadRepositoryLanguages(r.repo_id), `langs:${r.repo_id}`)
  const resolved = useUrRead(() => loadResolvedToken(r.repo_id), `token:${r.repo_id}`)
  const pol = r.selection_policy
  const policy = pol.inherited_from_tenant === true ? 'inherit' : pol.policy
  return (
    <>
      <p className="ur-hint ur-locked" data-route={PATCH_ROUTE} title={`Not served: ${PATCH_ROUTE}`}>
        {PATCH_WHY}
      </p>
      <div className="ur-cols">
        <div className="ur-stack">
          <Card title="Schedule and change trigger">
            <ScheduleLines r={r} />
          </Card>
          <Card
            title="Languages detected"
            action={
              <Button size="sm" busy={busy} onClick={onFull} title="Queues a full index run, which re-resolves every call site with the language servers (repo-index.md §3.5)">
                Re-run LSP pass
              </Button>
            }
          >
            <UrRegion
              state={langs.state}
              route="GET /v1/repositories/{repo_id}/languages"
              what="The languages table"
              onRetry={langs.reload}
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
                    <div className="ur-lang" key={l.language}>
                      <b>{l.language}</b>
                      <span>{l.grammar === null ? <Dash why="No tree-sitter grammar is bundled for this language" /> : <code>{l.grammar}</code>}</span>
                      <span>{l.server === null ? <Dash why="No language server is supported for this language" /> : <code>{l.server}</code>}</span>
                      <small>{langNote(l)}</small>
                      <ToneMark tone={LANG_TONE[l.status ?? ''] ?? 'unknown'}>{l.status === null ? 'not read' : l.status.replace(/_/g, ' ')}</ToneMark>
                    </div>
                  ))}
                </div>
              )}
            </UrRegion>
          </Card>
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
          <Card
            title="Selection policy"
            className="ur-policy"
            action={pol.inherited_from_tenant === null ? undefined : <Chip>{pol.inherited_from_tenant ? 'from the tenant default' : 'set for this repository'}</Chip>}
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
            <p className="ur-hint">{rulesetHint(pol.policy, r.default_branch)}</p>
          </Card>
          <Card title="Resolved token" className="ur-resolved" action={<UrNavButton to={GT_PAGE} go={go} size="sm">Git tokens</UrNavButton>}>
            <UrRegion state={resolved.state} route="GET /v1/git-tokens" what="The token that resolves here" onRetry={resolved.reload}>
              {(t) => <ResolvedRows t={t} />}
            </UrRegion>
          </Card>
        </div>
      </div>
    </>
  )
}

function langNote(l: { files: number | null; resolved: number | null; fallback: string | null }): ReactNode {
  const parts = [l.files !== null ? `${l.files} files` : null, l.resolved !== null ? `${pct(l.resolved)} resolved` : null].filter((x): x is string => x !== null)
  if (l.fallback !== null) parts.push(l.fallback)
  return parts.length === 0 ? <Dash why="No file count or resolution rate served" /> : parts.join(' · ')
}

/** What the picked policy expects of the branch rules (repo-index.md §4.4). The live ruleset is not read here. */
function rulesetHint(policy: string | null, branch: string | null): string {
  const on = branch ?? 'the default branch'
  if (policy === 'P1' || policy === 'P3') {
    return `The ruleset this policy expects on ${on}: swarmcloud/selected-tests required; full-suite jobs not required${policy === 'P3' ? ' (they still run when a change falls back to the full suite)' : ''}.`
  }
  if (policy === 'P2') return `The ruleset this policy expects on ${on}: the full suite required; swarmcloud/selected-tests reports first.`
  if (policy === 'off') return 'Selection is off: every pull request runs the full suite.'
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
          <span>your user token ({t.user_token.forge_login ?? 'account not read'}) is used only for attribution</span>
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
