/**
 * THE CACHE SECTION UNDER EACH INDEXED REPOSITORY (lane CACHE-4-CONSOLE, part
 * of #1835; docs/design/graph-answer-cache.md §5.1-§5.5). BGD picked mock-up C,
 * "live replicas, v2 from the start" (2026-10-27): the section reads every
 * swarm-graph replica live through swarm-api's
 * `GET /v1/repositories/{repo_id}/graph-cache`, not a daily summary.
 *
 * WHAT IT DRAWS (§5.2): four tiles of repository facts, one panel per replica,
 * and the shard list, folded. WHY EACH RULE:
 *
 *   * A REPLICA THAT WAS NOT READ IS ITS REASON, NEVER ZEROS (§5.4). A panel of
 *     zeros says the replica holds nothing for this repository; a timeout says
 *     nobody knows. Its panel names the replica and the reason and draws no
 *     figure at all.
 *   * SWARM-GRAPH NOT ANSWERING AT ALL (`replicas: []`, `stats: not_read`) still
 *     draws the latency tile: the uncached p95 is the index version's
 *     `answer_latency`, read from Firestore, not from a replica (§5.3).
 *   * THE REUSE TILE IS A DASH UNTIL CARRY FORWARD EXISTS (BGD Q4). 0% would say
 *     carry forward ran and reused nothing; the route leaves `carried` null
 *     until a replica counts one, and so does this tile.
 *   * A WINDOW SHORTER THAN 24 H IS LABELLED WITH ITS LENGTH ("over 5 h"): a
 *     replica restarted inside it and its counters started at zero (§5.2).
 *   * IT READS ONLY WHILE OPEN AND VISIBLE (§5.5): once on opening, then every
 *     30 s; nothing while folded or while `document.visibilityState` is
 *     `hidden`. Reopening, or the page becoming visible, reads once at once.
 *     The shard list is asked for (`shards=1`) only while it is unfolded, so an
 *     index of thousands of shards costs nothing while the list is closed.
 *   * A FAILED READ KEEPS THE LAST GOOD VALUES (§5.5), with the time they were
 *     read and the reason the new read failed; it never blanks them.
 *
 * Nothing about another tenant is drawn: the route returns none (§5.4). The
 * two MiB figures are each replica's totals across every tenant, shown because
 * a full cache is what makes this repository's answers miss.
 */
import { useEffect, useRef, useState } from 'react'
import { Dash, StatTile, UsageBar, buttonClass, type TileValue } from './components'
import {
  loadRepositoryGraphCache,
  type GraphCacheDoc,
  type GraphCacheFacts,
  type GraphCacheLatency,
  type GraphCacheReplica,
  type GraphCacheShardRow,
  type GraphCacheShards,
} from './api'
import type { ApiError, Result } from './fetch'
import { pct, shortSha, thousands } from './RepositoriesData'
import { useNow } from './useNow'

/** §5.5: the cadence while the section is open and the page visible. */
export const CACHE_REFRESH_MS = 30_000
/** The ages ("read 12 s ago") move every second: §5.5 shows each read's own time. */
const CACHE_AGE_TICK_MS = 1_000
/** The facts' window when no replica restarted inside it (§5.2). */
const FULL_WINDOW_HOURS = 24
const MiB = 1024 * 1024

export type GraphCacheLoad = (repoId: string, shardsPage: number | null) => Promise<Result<GraphCacheDoc>>

/**
 * Why a replica, or swarm-graph as a whole, was not read, in words: lane 1's
 * per-peer reasons (§5.3) and the route's own (graph_cache.py). An unknown
 * code is shown as itself rather than guessed at.
 */
const REASON_WORDS: Readonly<Record<string, string>> = {
  timeout: 'did not answer in time',
  refused: 'refused the connection',
  disabled: 'has its stats endpoint turned off',
  ticket_refused: 'refused the stats ticket',
  bad_response: 'answered with something that is not its stats',
  not_configured: 'this deployment has no address for the graph service’s stats yet',
  no_ticket_key: 'swarm-api holds no graph ticket key to ask with',
  no_index: 'no index is promoted',
  no_graph: 'the current index has no graph',
}

export function reasonWords(reason: string): string {
  return REASON_WORDS[reason] ?? reason
}

/** A span of seconds as the section says it: "12 s", "4 min", "3 h", "2 d". */
export function spanWords(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  if (s < 60) return `${s} s`
  if (s < 3600) return `${Math.floor(s / 60)} min`
  if (s < 48 * 3600) return `${Math.floor(s / 3600)} h`
  return `${Math.floor(s / 86400)} d`
}

function mib(bytes: number): number {
  return Math.round(bytes / MiB)
}

function bytesWords(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < MiB) return `${(n / 1024).toFixed(1)} KiB`
  return `${(n / MiB).toFixed(1)} MiB`
}

function msWords(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`
}

// ---------------------------------------------------------------------------
// Reading: open, visible, every 30 s (§5.5)
// ---------------------------------------------------------------------------

function useVisible(): boolean {
  const [visible, setVisible] = useState(() => typeof document === 'undefined' || document.visibilityState !== 'hidden')
  useEffect(() => {
    const on = () => setVisible(document.visibilityState !== 'hidden')
    document.addEventListener('visibilitychange', on)
    return () => document.removeEventListener('visibilitychange', on)
  }, [])
  return visible
}

interface CacheReads {
  /** The last good read and when it was read; kept through a failed one. */
  good: { doc: GraphCacheDoc; at: number } | null
  /** The newest read's failure, cleared by the next good one. */
  failed: { error: ApiError; at: number } | null
  reading: boolean
}

/**
 * One read at once whenever `active` turns true (opened, or visible again) or
 * the shard page asked for changes, then one every 30 s while it stays true.
 * Nothing at all while it is false.
 */
function useCacheReads(repoId: string, active: boolean, shardsPage: number | null, load: GraphCacheLoad): CacheReads {
  const [reads, setReads] = useState<CacheReads>({ good: null, failed: null, reading: false })
  const loadRef = useRef(load)
  loadRef.current = load
  useEffect(() => {
    if (!active) return
    let live = true
    const readOnce = () => {
      setReads((was) => ({ ...was, reading: true }))
      void loadRef.current(repoId, shardsPage).then((r) => {
        if (!live) return
        const at = Date.now()
        setReads((was) => {
          if (r.status === 'ok') return { good: { doc: r.data, at: r.fetchedAt }, failed: null, reading: false }
          if (r.status === 'stale') return { good: { doc: r.data, at: r.fetchedAt }, failed: { error: r.error, at }, reading: false }
          if (r.status === 'error') return { ...was, failed: { error: r.error, at }, reading: false }
          return { ...was, reading: false }
        })
      })
    }
    readOnce()
    const timer = setInterval(readOnce, CACHE_REFRESH_MS)
    return () => {
      live = false
      clearInterval(timer)
    }
  }, [active, repoId, shardsPage])
  return reads
}

// ---------------------------------------------------------------------------
// The section
// ---------------------------------------------------------------------------

/**
 * The Cache section of one repository's card, folded by default (§5.1).
 * `indexSha` is the registration's `index.current_sha`: null is "No index
 * promoted yet", and nothing is read. `load` is the route's fetcher.
 */
export function RepoGraphCache({
  repoId,
  indexSha,
  load = loadRepositoryGraphCache,
}: {
  repoId: string
  indexSha: string | null
  load?: GraphCacheLoad
}) {
  const [open, setOpen] = useState(false)
  const [shardsOpen, setShardsOpen] = useState(false)
  const [page, setPage] = useState(1)
  const visible = useVisible()
  const promoted = indexSha !== null
  const active = open && visible && promoted
  const reads = useCacheReads(repoId, active, shardsOpen ? page : null, load)
  const bodyId = `ur-cache-${repoId}`
  return (
    <section className="ur-cache" data-cache-section="" data-open={open ? 'true' : 'false'}>
      <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm' }, false, 'ur-cache-toggle')} aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen((o) => !o)}>
        Cache <span aria-hidden="true">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div id={bodyId} className="ur-cache-body">
          {!promoted ? (
            <p className="ur-small ur-mu">No index promoted yet</p>
          ) : (
            <CacheBody
              reads={reads}
              visible={visible}
              shardsOpen={shardsOpen}
              onShards={() => setShardsOpen((s) => !s)}
              page={page}
              onPage={setPage}
            />
          )}
        </div>
      )}
    </section>
  )
}

function CacheBody({
  reads,
  visible,
  shardsOpen,
  onShards,
  page,
  onPage,
}: {
  reads: CacheReads
  visible: boolean
  shardsOpen: boolean
  onShards: () => void
  page: number
  onPage: (p: number) => void
}) {
  const now = useNow(CACHE_AGE_TICK_MS)
  const { good, failed } = reads
  if (good === null) {
    if (failed !== null) {
      return (
        <p className="ur-small" data-read-failed="">
          The cache could not be read: {failed.error.message}
        </p>
      )
    }
    return <p className="ur-small ur-mu">{visible ? 'Reading the cache…' : 'Read when the page is visible again.'}</p>
  }
  const doc = good.doc
  return (
    <>
      <p className="ur-small ur-mu" data-row="read-age">
        <span data-read-age="">read {spanWords((now - good.at) / 1000)} ago</span>
        {failed !== null && (
          <span data-read-failed="">
            {' '}
            · the latest read failed {spanWords((now - failed.at) / 1000)} ago: {failed.error.message}
          </span>
        )}
        {!visible && ' · paused while the page is hidden'}
      </p>
      {doc.stats.state === 'not_read' && (
        <p className="ur-small" data-stats="not_read" data-reason={doc.stats.reason}>
          swarm-graph was not read: {reasonWords(doc.stats.reason)}.
        </p>
      )}
      <div className="c-tiles" data-row="facts">
        <StatTile label="Answer hit rate, 24 h" tile={hitRateTile(doc.facts, doc.stats)} />
        <StatTile label="Reused from the last index" tile={reuseTile(doc.facts, doc.stats)} />
        <StatTile label="p95 served / uncached" tile={latencyTile(doc.facts, doc.latency)} />
        <StatTile label="Last promotion warm-up" tile={warmTile(doc.facts, doc.stats)} />
      </div>
      {doc.replicas.length > 0 && (
        <div className="ur-cache-replicas" data-row="replicas" style={REPLICA_GRID}>
          {doc.replicas.map((r) => (
            <ReplicaPanel key={r.replica} r={r} />
          ))}
        </div>
      )}
      <ShardList open={shardsOpen} onToggle={onShards} shards={shardsOpen ? doc.shards : null} page={page} onPage={onPage} />
    </>
  )
}

// ---------------------------------------------------------------------------
// The four tiles (§5.2)
// ---------------------------------------------------------------------------

type Stats = GraphCacheDoc['stats']

function notReadWhy(stats: Stats): string {
  return stats.state === 'not_read' ? `swarm-graph was not read: ${reasonWords(stats.reason)}` : 'No replica was read'
}

/** "over 5 h" when a replica restarted inside the 24 h, never presented as 24 h. */
function windowNote(f: GraphCacheFacts): string | null {
  return f.window_hours !== null && f.window_hours < FULL_WINDOW_HOURS ? `over ${f.window_hours} h` : null
}

function withWindow(words: string, f: GraphCacheFacts): string {
  const w = windowNote(f)
  return w === null ? words : `${words} · ${w}`
}

function hitRateTile(f: GraphCacheFacts, stats: Stats): TileValue {
  if (f.answers === null) return { kind: 'unknown', why: notReadWhy(stats) }
  if (f.hit_rate === null) return { kind: 'unknown', why: withWindow('No answers yet', f) }
  return { kind: 'value', value: pct(f.hit_rate), note: withWindow(`of ${thousands(f.answers)} answers`, f) }
}

function reuseTile(f: GraphCacheFacts, stats: Stats): TileValue {
  // BGD Q4: a dash until carry forward exists, never 0%, and no carried count.
  if (f.carried === null || f.reused_share === null) {
    return { kind: 'unknown', why: f.answers === null ? notReadWhy(stats) : 'Carry forward is not built yet' }
  }
  return { kind: 'value', value: pct(f.reused_share), note: withWindow(`${thousands(f.carried)} carried`, f) }
}

function latencyTile(f: GraphCacheFacts, latency: GraphCacheLatency | null): TileValue {
  const served = f.served_p95_ms === null ? null : msWords(f.served_p95_ms)
  if (latency === null || latency.state === 'unmeasured') {
    if (served === null) return { kind: 'unknown', why: 'Not measured' }
    return { kind: 'partial', value: `${served} / —`, missed: 'uncached: not measured' }
  }
  const ratio = latency.reference_ms !== null && latency.reference_ms > 0 ? ` · ${(latency.p95_ms / latency.reference_ms).toFixed(1)}× reference` : ''
  const uncached = msWords(latency.p95_ms)
  if (served === null) return { kind: 'partial', value: `— / ${uncached}`, missed: `served: not read${ratio}` }
  return { kind: 'value', value: `${served} / ${uncached}`, note: withWindow(`served / uncached${ratio}`, f) }
}

function warmTile(f: GraphCacheFacts, stats: Stats): TileValue {
  const w = f.last_warm
  if (w === null) {
    return { kind: 'unknown', why: f.answers === null ? notReadWhy(stats) : 'No replica read recorded a warm of the current index' }
  }
  const replayed = w.replayed === null ? 'questions replayed not recorded' : `${thousands(w.replayed)} questions replayed`
  return { kind: 'value', value: w.ms === null ? '—' : msWords(w.ms), note: replayed }
}

// ---------------------------------------------------------------------------
// One panel per replica (§5.2)
// ---------------------------------------------------------------------------

// Each replica is the shared bordered .c-card; the panels stack with a gap, and a
// replica that was not read gets a dashed border, as an unknown .c-tile does.
const REPLICA_GRID = { display: 'grid', gap: 8 } as const
const NOT_READ_PANEL = { borderStyle: 'dashed', borderColor: 'var(--ctl-absent)' } as const

function ReplicaPanel({ r }: { r: GraphCacheReplica }) {
  if (r.state === 'not_read') {
    // No figure at all: a replica nobody read holds an unknown, not zero (§5.4).
    return (
      <div className="c-card ur-cache-replica is-not-read" data-replica={r.replica} data-state="not_read" data-reason={r.reason} style={NOT_READ_PANEL}>
        <b>{r.replica}</b>
        <p className="ur-small">
          Not read: it {reasonWords(r.reason)} ({r.reason}).
        </p>
      </div>
    )
  }
  const started = r.started_at === null ? null : new Date(r.started_at)
  return (
    <div className="c-card ur-cache-replica" data-replica={r.replica} data-state="read">
      <b>{r.replica}</b>
      <p className="ur-small ur-mu">
        up {r.uptime_seconds === null ? <Dash why="Its start time was not served" /> : spanWords(r.uptime_seconds)}
        {started !== null && !Number.isNaN(started.getTime()) && (
          <>
            {' '}
            · started <span title={started.toISOString()}>{started.toLocaleString()}</span>
          </>
        )}
      </p>
      <UsageBar
        label="Answers"
        used={r.result_bytes === null ? null : mib(r.result_bytes)}
        ceiling={r.result_bound === null ? null : mib(r.result_bound)}
        unit="MiB"
        why="not served"
      />
      <UsageBar
        label="Shards"
        used={r.shard_bytes === null ? null : mib(r.shard_bytes)}
        ceiling={r.shard_bound === null ? null : mib(r.shard_bound)}
        unit="MiB"
        why="not served"
      />
      <p className="ur-small" data-row="this-repository">
        For this repository:{' '}
        {r.answers_hot === null ? <Dash why="Not served" /> : <b>{thousands(r.answers_hot)}</b>} answers and{' '}
        {r.shards_hot === null ? <Dash why="Not served" /> : <b>{thousands(r.shards_hot)}</b>} shards hot
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The shard list, folded by default (§5.2)
// ---------------------------------------------------------------------------

function shardTitle(shards: GraphCacheShards | null): string {
  if (shards === null || shards.state !== 'read') return 'Shards'
  const changed = shards.changed === null ? '' : ` · ${thousands(shards.changed)} changed by the last merge`
  return `Shards (${thousands(shards.total)})${changed}`
}

function ShardList({
  open,
  onToggle,
  shards,
  page,
  onPage,
}: {
  open: boolean
  onToggle: () => void
  shards: GraphCacheShards | null
  page: number
  onPage: (p: number) => void
}) {
  return (
    <div className="ur-cache-shards" data-row="shards">
      <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm' }, false, 'ur-cache-toggle')} aria-expanded={open} onClick={onToggle}>
        {shardTitle(shards)} <span aria-hidden="true">{open ? '▾' : '▸'}</span>
      </button>
      {open && <ShardRows shards={shards} page={page} onPage={onPage} />}
    </div>
  )
}

function ShardRows({ shards, page, onPage }: { shards: GraphCacheShards | null; page: number; onPage: (p: number) => void }) {
  if (shards === null) return <p className="ur-small ur-mu">Reading the shard list…</p>
  if (shards.state === 'not_read') return <p className="ur-small">The shard list was not read: {reasonWords(shards.reason)}.</p>
  if (shards.rows.length === 0) return <p className="ur-small ur-mu">This index has no shards on this page.</p>
  const pages = Math.max(1, Math.ceil(shards.total / shards.page_rows))
  const replicas = shards.rows[0]?.replicas.map((m) => m.replica) ?? []
  return (
    <>
      {shards.comparison !== 'read' && (
        <p className="ur-small ur-mu">The previous index could not be opened ({shards.comparison}), so the last merge is not known.</p>
      )}
      <div className="c-tbl">
        <table aria-label="Shards of the current index">
          <thead>
            <tr>
              <th>Layer</th>
              <th>Module</th>
              <th className="is-num">Size</th>
              <th>Last merge</th>
              <th>Content hash</th>
              {replicas.map((name) => (
                <th key={name}>{name}</th>
              ))}
              <th className="is-num">Hits, 24 h</th>
              <th>Last read</th>
            </tr>
          </thead>
          <tbody>
            {shards.rows.map((row) => (
              <ShardRow key={`${row.layer}/${row.module}`} row={row} replicas={replicas} />
            ))}
          </tbody>
        </table>
      </div>
      {pages > 1 && (
        <p className="ur-small" data-row="shard-pages">
          <button type="button" className={buttonClass({ size: 'sm' })} disabled={page <= 1} onClick={() => onPage(page - 1)}>
            Previous
          </button>{' '}
          Page {page} of {pages}{' '}
          <button type="button" className={buttonClass({ size: 'sm' })} disabled={page >= pages} onClick={() => onPage(page + 1)}>
            Next
          </button>
        </p>
      )}
    </>
  )
}

function ShardRow({ row, replicas }: { row: GraphCacheShardRow; replicas: readonly string[] }) {
  const hex = row.blob.startsWith('sha256:') ? row.blob.slice('sha256:'.length) : row.blob
  return (
    <tr data-shard-row="" data-last-merge={row.last_merge ?? 'unknown'}>
      <td>{row.layer}</td>
      <td>{row.module}</td>
      <td className="is-num">{row.raw_bytes === null ? <Dash why="Not in the manifest" /> : bytesWords(row.raw_bytes)}</td>
      <td>{row.last_merge === null ? <Dash why="There is no previous index to compare with" /> : row.last_merge}</td>
      <td>
        <span className="ur-sha" title={hex}>
          {shortSha(hex)}
        </span>
      </td>
      {replicas.map((name) => {
        const m = row.replicas.find((x) => x.replica === name)
        return <td key={name}>{m === undefined ? <Dash why="Not read on this replica" /> : m.hot ? 'hot' : 'cold'}</td>
      })}
      <td className="is-num">{row.hits_24h === null ? <Dash why="No replica was read" /> : thousands(row.hits_24h)}</td>
      <td>{row.last_read === null ? <Dash why="Not read in the window" /> : <LastRead at={row.last_read} />}</td>
    </tr>
  )
}

function LastRead({ at }: { at: string }) {
  const now = useNow(CACHE_AGE_TICK_MS)
  const t = Date.parse(at)
  if (Number.isNaN(t)) return <Dash why="Not a time" />
  return <span title={at}>{spanWords((now - t) / 1000)} ago</span>
}
