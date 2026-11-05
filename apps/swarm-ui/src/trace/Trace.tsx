// THE TRANSCRIPT AS A TRACE (owner pick, 2026-11-05, part of #2688): Logs ›
// Transcript as a trace viewer. On the left
// a compact timeline of every event, one row each; on the right the picked
// event in full. Read from the same `GET /v1/tasks/{id}/transcript` the
// Transcript stream reads, with `include_raw=true` for the fields only the
// record carries (the working directory, progress seconds, an edit's patch,
// token usage).

import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import { artifactRawUrl, loadArtifactListing } from '../api'
import type { Result } from '../fetch'
import { Markdown } from '../ArtifactViewer'
import { ARTIFACTS_POLL_MS } from '../Artifacts'
import { TERMINAL_STATES, type Task, type TaskTranscript } from '../types'
import { listSnap, setListSnap, useListSnap } from '../listSnap'
import { buildTrace, dur, inFilter, inputOf, patchOf, rawOf, setTraceWide, shorten, toolShort, traceMounted, useTraceWide, type Ev, type EvKind, type Filter } from './model'
import { ReadFacts, StreamStatus, WindowNotes } from './Notes'
import './trace.css'

/** A live run's transcript is read again as often as the Logs tab reads its streams. */
const POLL_MS = ARTIFACTS_POLL_MS

function Icon({ kind, error }: { kind: EvKind; error: boolean }) {
  const p = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.6, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const }
  const body: Record<EvKind, ReactNode> = {
    message: <path {...p} d="M3 4.5h10v6.5H7l-3 2.5V11H3z" />,
    tool: <><path {...p} d="M3.5 5l3 3-3 3" /><path {...p} d="M8.5 11.5h4" /></>,
    edit: <><path {...p} d="M10.5 3.5l2 2L6 12H4v-2z" /></>,
    test: <><path {...p} d="M6 3h4M6.8 3v4L3.8 12.2c-.3.6.1 1.3.8 1.3h6.8c.7 0 1.1-.7.8-1.3L9.2 7V3" /></>,
    browser: <><rect {...p} x="2.5" y="3.5" width="11" height="9" rx="1.5" /><path {...p} d="M2.5 6.2h11" /></>,
    thinking: <><circle cx="4.5" cy="8" r="1" fill="currentColor" /><circle cx="8" cy="8" r="1" fill="currentColor" /><circle cx="11.5" cy="8" r="1" fill="currentColor" /></>,
    start: <path {...p} d="M5 3.5l7 4.5-7 4.5z" />,
    end: <><path {...p} d="M4 13.5V3M4 3.5h7.5l-1.5 2.5 1.5 2.5H4" /></>,
    system: <><circle {...p} cx="8" cy="8" r="2" /><path {...p} d="M8 2.5v2M8 11.5v2M2.5 8h2M11.5 8h2" /></>,
    other: <><path {...p} d="M6.2 6.2a1.9 1.9 0 113 1.5c-.7.4-1.2.9-1.2 1.6" /><circle cx="8" cy="11.8" r=".8" fill="currentColor" /></>,
  }
  return (
    <svg className={`trc-ic is-${error ? 'error' : kind}`} viewBox="0 0 16 16" width="16" height="16" aria-hidden>
      {error ? <><path {...p} d="M8 2.5l6 10.5H2z" /><path {...p} d="M8 6.5v3" /><circle cx="8" cy="11.4" r=".8" fill="currentColor" /></> : body[kind]}
    </svg>
  )
}

const KIND_WORD: Record<EvKind, string> = {
  message: 'message', tool: 'tool', edit: 'edit', test: 'test', browser: 'browser', thinking: 'thinking', start: 'start', end: 'result', system: 'system', other: 'not recognised',
}

/** Drawn, not typed: a tick, a cross or a dot that every font shows. */
function StatusMark({ d, dot }: { d?: string; dot?: boolean }) {
  return (
    <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden>
      {dot ? <circle cx="6" cy="6" r="3" fill="currentColor" /> : <path d={d} fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />}
    </svg>
  )
}

function Status({ ev }: { ev: Ev }) {
  if (ev.status === 'ok') {
    return (
      <span className="trc-st is-ok" role="img" aria-label="ok" title="Its result is not marked as an error">
        <StatusMark d="M2.5 6.4l2.3 2.3L9.5 3.6" />
      </span>
    )
  }
  if (ev.status === 'error') {
    return (
      <span className="trc-st is-err" role="img" aria-label="error" title="Marked is_error by the CLI">
        <StatusMark d="M3 3l6 6M9 3l-6 6" />
      </span>
    )
  }
  if (ev.status === 'running') {
    return (
      <span className="trc-st is-run" role="img" aria-label="no result yet" title="No result in this read yet">
        <StatusMark dot />
      </span>
    )
  }
  return <span className="trc-st" />
}

function Secs({ ev }: { ev: Ev }) {
  if (ev.secs === null) return <span className="trc-dur" />
  return ev.secs.from === 'progress' ? (
    <span className="trc-dur" title={`The CLI's last progress report for this call said ${ev.secs.value} s; it reports every few seconds, so the call took at least this.`}>
      ≥{dur(ev.secs.value)}
    </span>
  ) : (
    <span className="trc-dur" title="The run's duration_ms, from the result record">{dur(ev.secs.value)}</span>
  )
}

/** The needle marked in a text, case-insensitively. */
function Marked({ text, needle }: { text: string; needle: string }) {
  if (needle === '') return <>{text}</>
  const low = text.toLowerCase()
  const out: ReactNode[] = []
  let at = 0
  for (let i = low.indexOf(needle); i !== -1; i = low.indexOf(needle, i + needle.length)) {
    out.push(text.slice(at, i), <mark key={i}>{text.slice(i, i + needle.length)}</mark>)
    at = i + needle.length
  }
  out.push(text.slice(at))
  return <>{out}</>
}

function Out({ text, needle, tone }: { text: string; needle: string; tone?: 'console' | 'test' }) {
  const lines = text.split('\n')
  return (
    <pre className="trc-pre">
      {lines.map((l, i) => {
        const cls =
          tone === 'console'
            ? /^\[ERROR\]/.test(l) ? 'is-err' : /^\[WARNING\]/.test(l) ? 'is-warn' : ''
            : /^(E |>\s|Traceback)/.test(l) ? 'is-err is-stack' : /^(FAILED |Exit code [1-9]|\w*Error:)/.test(l) || / failed[, ]/.test(l) ? 'is-err' : / passed in /.test(l) && !/failed/.test(l) ? 'is-ok' : ''
        return (
          <span key={i} className={`trc-ln ${cls}`}>
            <Marked text={l} needle={needle} />
            {'\n'}
          </span>
        )
      })}
    </pre>
  )
}

function Diff({ ev, changesHref, path, outside }: { ev: Ev; changesHref: string | null; path: string; outside: boolean }) {
  const hunks = patchOf(ev)
  if (hunks === null) return <p className="trc-dim">no change recorded</p>
  return (
    <div className="trc-diff">
      <div className="trc-diff-head">
        <span className="mono trc-diff-path" title={outside ? 'Written outside the repository: not on the Changes tab' : undefined}>{path}</span>
        {changesHref !== null && (
          <a className="trc-link trc-btn" href={changesHref} onClick={(e) => { e.preventDefault(); window.history.pushState(null, '', changesHref); window.dispatchEvent(new PopStateEvent('popstate')) }}>
            Open in Changes ›
          </a>
        )}
      </div>
      {hunks.map((h, hi) => {
        let o = h.oldStart ?? 0
        let n = h.newStart ?? 0
        return (
          <table key={hi} className="trc-hunk">
            <tbody>
              {h.oldStart !== null && (
                <tr className="is-hunk"><td colSpan={3}>@@ −{h.oldStart} +{h.newStart} @@</td></tr>
              )}
              {h.lines.map((l, i) => {
                const k = l[0] === '+' ? 'add' : l[0] === '-' ? 'del' : 'ctx'
                const left = k === 'add' || h.oldStart === null ? '' : String(o++)
                const right = k === 'del' || h.newStart === null ? '' : String(n++)
                return (
                  <tr key={i} className={`is-${k}`}>
                    <td className="trc-no">{left}</td>
                    <td className="trc-no">{right}</td>
                    <td className="trc-code"><span className="trc-sign">{k === 'add' ? '+' : k === 'del' ? '−' : ' '}</span>{l.slice(1)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )
      })}
    </div>
  )
}

/** The artifact name a browser tool's result points at (`…/artifacts/<name>.png`), or null. */
export function shotName(out: string | null): string | null {
  const path = out !== null ? (/\]\(([^)]+\.png)\)/.exec(out)?.[1] ?? null) : null
  return path !== null && path.includes('/artifacts/') ? path.slice(path.indexOf('/artifacts/') + '/artifacts/'.length) : null
}

/**
 * A SCREENSHOT IS A SECOND READ: the transcript holds no image bytes, only the
 * artifact the tool result names. Drawn from the run's artifacts when the
 * listing has it; said in words when it does not.
 */
function Shot({ taskId, name, names }: { taskId: string; name: string; names: ReadonlySet<string> | null }) {
  if (names === null) return null
  if (!names.has(name)) return <p className="trc-dim">{name} is not in this run&rsquo;s artifacts.</p>
  return (
    <figure className="trc-shot">
      <a className="trc-shot-frame" href={artifactRawUrl(taskId, name, 'inline')} target="_blank" rel="noreferrer">
        <img src={artifactRawUrl(taskId, name, 'inline')} alt={`Screenshot ${name}`} loading="lazy" />
      </a>
      <figcaption>
        <span className="mono">{name}</span> · from Artifacts
      </figcaption>
    </figure>
  )
}

/** A record as stored, indented; a line that is not JSON is shown as it is. */
function pretty(r: string): string {
  try {
    return JSON.stringify(JSON.parse(r), null, 2)
  } catch {
    return r
  }
}

function Facts({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="trc-facts">
      {rows.map(([k, v]) => (
        <Fragment key={k}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </Fragment>
      ))}
    </dl>
  )
}

function Detail({
  ev, trace, needle, lastAnswer, onPick, changesBase, onKeep, kept, onUnkeep, compact, taskId, shots,
}: {
  taskId: string
  shots: ReadonlySet<string> | null
  ev: Ev
  trace: ReturnType<typeof buildTrace>
  needle: string
  lastAnswer: Ev | null
  onPick: (key: string) => void
  changesBase: string | null
  onKeep?: () => void
  kept?: boolean
  onUnkeep?: () => void
  compact?: boolean
}) {
  const [record, setRecord] = useState(false)
  const card = useRef<HTMLElement>(null)
  // AN ERROR OPENS AT ITS STACK: the first `>` or `E ` line of its output is brought into view in its own scroller.
  useLayoutEffect(() => {
    if (ev.status !== 'error') return
    const line = card.current?.querySelector<HTMLElement>('.trc-ln.is-stack')
    const slot = card.current?.closest<HTMLElement>('.trc-slot')
    if (!line || !slot) return
    const top = line.getBoundingClientRect().top - slot.getBoundingClientRect().top
    slot.scrollTop += top - 96
  }, [ev.key, ev.status])
  const short = (t: string) => shorten(t, trace.cwd, trace.root)
  const inp = inputOf(ev.call)
  const out = ev.result?.tool_result?.content ?? null
  const name = ev.call?.tool?.name ?? null
  const raws = [ev.call ?? ev.main, ...ev.progressSteps, ...(ev.result ? [ev.result] : [])].map((s) => s.raw).filter((r): r is string => r !== null)
  const uniqueRaws = raws.filter((r, i) => raws.indexOf(r) === i)
  const usage = (rawOf(ev.main)?.message as Record<string, unknown> | undefined)?.usage as Record<string, number> | undefined
  let body: ReactNode = null
  if (ev.kind === 'message') {
    body = <div className="trc-md"><Markdown source={ev.main.text ?? ''} /></div>
  } else if (ev.kind === 'thinking') {
    body = ev.main.text ? <pre className="trc-pre">{ev.main.text}</pre> : <p className="trc-dim">The CLI recorded this block without its text.</p>
  } else if (ev.kind === 'start') {
    const raw = rawOf(ev.main)
    body = (
      <Facts rows={[
        ['model', String(ev.main.meta?.model ?? '—')],
        ['tools', String(ev.main.meta?.tool_count ?? '—')],
        ['permissions', String(ev.main.meta?.permission_mode ?? '—')],
        ['working dir', <span className="mono" key="c">{String(raw?.cwd ?? '—')}</span>],
      ]} />
    )
  } else if (ev.kind === 'end') {
    const m = ev.main.meta ?? {}
    const same = lastAnswer !== null && ev.main.text !== null && ev.main.text === lastAnswer.main.text
    body = (
      <>
        <Facts rows={[
          ['outcome', <span key="o" className={m.is_error === true ? 'trc-bad' : 'trc-good'}>{String(m.subtype ?? '—')}{m.is_error === true ? ' · is_error' : ''}</span>],
          ['turns', String(m.num_turns ?? '—')],
          ['duration', typeof m.duration_ms === 'number' ? dur(m.duration_ms / 1000) : '—'],
          ['cost', typeof m.total_cost_usd === 'number' ? `$${m.total_cost_usd.toFixed(2)}` : '—'],
          ['answer', ev.main.text === null || ev.main.text === '' ? 'none recorded' : same ? (
            <button key="a" type="button" className="trc-link" onClick={() => onPick(lastAnswer!.key)}>same as #{lastAnswer!.n}, the last message ↑</button>
          ) : 'below'],
        ]} />
        {ev.main.text && !same && <div className="trc-md"><Markdown source={ev.main.text} /></div>}
      </>
    )
  } else if (ev.kind === 'edit') {
    const path = short(String(inp.file_path ?? inp.notebook_path ?? ''))
    const inRepo = trace.cwd !== null && String(inp.file_path ?? '').startsWith(`${trace.cwd}/`)
    body = (
      <>
        <Diff ev={ev} path={path} outside={!inRepo} changesHref={inRepo && changesBase !== null ? `${changesBase}/changes/${encodeURIComponent(path)}${window.location.search}` : null} />
        {out !== null && !compact && (
          <details className="trc-more">
            <summary>The tool&rsquo;s reply</summary>
            <Out text={short(out)} needle={needle} />
          </details>
        )}
      </>
    )
  } else if (ev.call !== null || ev.result !== null) {
    const isBash = name === 'Bash'
    const shot = shotName(out)
    const fields = Object.entries(inp).filter(([k]) => !(isBash && (k === 'command' || k === 'description')))
    body = (
      <>
        {isBash && (
          <section className="trc-sec">
            <h4>Command</h4>
            <pre className="trc-pre trc-cmd"><Marked text={short(String(inp.command ?? ''))} needle={needle} /></pre>
          </section>
        )}
        {fields.length > 0 && (
          <Facts rows={fields.map(([k, v]) => [k, <span key={k} className="mono">{typeof v === 'string' ? short(v) : JSON.stringify(v)}</span>])} />
        )}
        {shot !== null && <Shot taskId={taskId} name={shot} names={shots} />}
        <section className="trc-sec">
          <h4>{ev.status === 'error' ? 'Output · error' : 'Output'}{ev.result?.tool_result?.images ? ` · ${ev.result.tool_result.images} image` : ''}</h4>
          {out === null ? (
            <p className="trc-dim">{ev.status === 'running' ? 'no result in this read yet' : 'no output recorded'}</p>
          ) : (
            <Out text={short(out)} needle={needle} tone={name !== null && /console_messages/.test(name) ? 'console' : 'test'} />
          )}
        </section>
      </>
    )
  } else if (ev.kind === 'system' && ev.progressSteps.length > 0) {
    body = <p className="trc-dim">its call is not in this read</p>
  } else if (ev.kind === 'system' && ev.main.meta) {
    body = <Facts rows={Object.entries(ev.main.meta).map(([k, v]) => [k, String(v)])} />
  } else if (ev.kind !== 'other' && ev.main.text) {
    body = <pre className="trc-pre"><Marked text={ev.main.text} needle={needle} /></pre>
  }
  const desc = typeof inp.description === 'string' ? inp.description : null
  const showRecord = record || ev.kind === 'other'
  return (
    <article ref={card} className={`trc-card${ev.status === 'error' ? ' is-error' : ''}`} aria-label={`Event ${ev.n}`}>
      <header className="trc-card-head">
        <Icon kind={ev.kind} error={ev.status === 'error'} />
        <div className="trc-card-title">
          <h3 className={ev.call !== null && name === 'Bash' ? 'mono' : ''}>
            {ev.kind === 'message' ? 'Message' : ev.kind === 'edit' ? ev.label : ev.call !== null ? toolShort(name ?? 'tool') : ev.label}
          </h3>
          <p className="trc-card-sub">
            <span>#{ev.n} · {KIND_WORD[ev.kind]}</span>
            {ev.secs !== null && <> · <Secs ev={ev} /></>}
            {ev.progress > 0 && <span title="tool_progress records for this call, folded into it"> · {ev.progress} progress</span>}
            {usage?.output_tokens !== undefined && <span title="message.usage from the record"> · {usage.output_tokens} output tokens</span>}
            {desc && <span className="trc-desc"> · {desc}</span>}
          </p>
        </div>
        <div className="trc-card-acts">
          {onKeep && !kept && <button type="button" className="trc-btn" onClick={onKeep} title="Keep this event open below while you pick another">Keep open</button>}
          {onUnkeep && <button type="button" className="trc-btn" onClick={onUnkeep} aria-label="Close this kept event" title="Close">×</button>}
          {ev.kind !== 'other' && uniqueRaws.length > 0 && (
            <button type="button" className={`trc-btn${record ? ' is-on' : ''}`} aria-pressed={record} onClick={() => setRecord((r) => !r)} title="The stream-json records behind this event, as stored">
              {'{ }'}
            </button>
          )}
        </div>
      </header>
      <div className="trc-card-body">
        {body}
        {showRecord && (
          <section className="trc-sec">
            <h4>Record{uniqueRaws.length > 1 ? `s · ${uniqueRaws.length}` : ''}</h4>
            <pre className="trc-pre trc-raw">{uniqueRaws.length > 0 ? uniqueRaws.map(pretty).join('\n') : pretty(JSON.stringify(ev.main.meta ?? {}))}</pre>
          </section>
        )}
      </div>
    </article>
  )
}

/**
 * `streams` is the Transcript / Agent stdout / Agent stderr / Runner switch and
 * `tools` the attempt picker, both owned by the Logs tab. `load` reads the
 * window the tab picked (`readTranscriptWindow`, with the records), for the
 * attempt `attemptId` (null: the latest) and from its first window when
 * `fromStart`.
 */
export function Trace({
  task, streams, tools, attemptId, fromStart, onFromStart, load, query, onQuery,
}: {
  /** The tab's search, kept by the tab so it outlives a switch of view. */
  query: string
  onQuery: (next: string) => void
  task: Task
  streams: ReactNode
  tools?: ReactNode
  attemptId: string | null
  fromStart: boolean
  onFromStart: (next: boolean) => void
  load: () => Promise<Result<TaskTranscript>>
}) {
  const [read, setRead] = useState<TaskTranscript | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const [fetchedAt, setFetchedAt] = useState<number | null>(null)
  const live = !TERMINAL_STATES.has(task.state)
  const loader = useRef(load)
  loader.current = load
  useEffect(() => {
    let gone = false
    setRead(null)
    setFailed(null)
    const go = () =>
      loader.current().then((r) => {
        if (gone) return
        if (r.status === 'ok') {
          setRead(r.data)
          setFetchedAt(r.fetchedAt)
          setFailed(null)
        } else if (r.status === 'stale') setRead(r.data)
        else if (r.status === 'error') setFailed(r.error.message)
      })
    void go()
    if (!live) return () => { gone = true }
    const id = setInterval(go, POLL_MS)
    return () => {
      gone = true
      clearInterval(id)
    }
  }, [task.id, live, attemptId, fromStart])

  const trace = useMemo(() => buildTrace(read?.steps ?? []), [read])
  const events = trace.events
  // THE RUN'S ARTIFACTS, read once a browser step names a screenshot.
  const wantsShots = events.some((e) => shotName(e.result?.tool_result?.content ?? null) !== null)
  const [shots, setShots] = useState<ReadonlySet<string> | null>(null)
  useEffect(() => {
    if (!wantsShots) return
    let gone = false
    void loadArtifactListing(task.id).then((r) => {
      if (!gone) setShots(new Set(r.status === 'ok' ? r.data.artifacts.map((a) => a.name) : []))
    })
    return () => {
      gone = true
    }
  }, [task.id, wantsShots])
  const [filter, setFilter] = useState<Filter>('all')
  const needle = query.trim().toLowerCase()
  const [picked, setPicked] = useState<string | null>(null)
  const [kept, setKept] = useState<string[]>([])
  const [phoneDetail, setPhoneDetail] = useState(false)
  const traceWide = useTraceWide()
  const snap = useListSnap()
  const lastAnswer = useMemo(() => [...events].reverse().find((e) => e.kind === 'message' && e.main.role === 'assistant') ?? null, [events])
  // AT REST: a live run follows its newest event; a finished one opens on its last message, the answer.
  const following = live && picked === null
  const restKey = live ? (events[events.length - 1]?.key ?? null) : (lastAnswer?.key ?? events[events.length - 1]?.key ?? null)
  const selKey = picked ?? restKey
  const sel = events.find((e) => e.key === selKey) ?? null
  const shown = events.filter((e) => inFilter(e, filter))
  const hits = needle === '' ? [] : shown.filter((e) => e.hay.includes(needle))
  const errors = events.filter((e) => e.status === 'error')
  const counts = { all: events.length, messages: 0, tools: 0, edits: 0, errors: errors.length }
  for (const e of events) {
    if (inFilter(e, 'messages')) counts.messages++
    if (inFilter(e, 'tools')) counts.tools++
    if (inFilter(e, 'edits')) counts.edits++
  }

  const root = useRef<HTMLElement>(null)
  // FULL WIDTH WHILE A TRANSCRIPT IS READ. In the Work inspector the list
  // beside it folds to a strip (work/Inspector.tsx, `is-trwide`); beside the
  // Agents list (AgentSplit) that list folds to its own strip, unremembered,
  // and opens again to where it was when the trace goes.
  const [inInspector, setInInspector] = useState(true)
  useLayoutEffect(() => {
    const docked = root.current?.closest('.wi') != null
    setInInspector(docked)
    if (docked) return traceMounted()
    const was = listSnap()
    if (was !== 'strip') setListSnap('strip', false)
    return () => {
      if (was !== 'strip' && listSnap() === 'strip') setListSnap(was, false)
    }
  }, [])
  const wide = inInspector ? traceWide : snap === 'strip'
  const fullWidth = () => (inInspector ? setTraceWide(true) : setListSnap('strip', false))
  const list = useRef<HTMLOListElement>(null)
  const search = useRef<HTMLInputElement>(null)
  const [layout, setLayout] = useState<'side' | 'stack' | 'phone'>('side')
  const headFolded = useRef(false)
  const phoneViewport = typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(max-width: 720px)').matches
  useLayoutEffect(() => {
    const el = root.current
    if (el === null || typeof ResizeObserver === 'undefined') return
    const put = () => {
      const box = el.getBoundingClientRect()
      setLayout(box.width < 560 ? 'phone' : box.width < 860 ? 'stack' : 'side')
      // THE TRACE FILLS WHAT IS LEFT OF THE WINDOW under the agent's head, so both panes end in view.
      const drawer = el.closest('.ctl-drawer')
      const top = box.top + (drawer?.scrollTop ?? 0)
      // BESIDE THE AGENT LIST (a narrow dock, not a phone) the head would leave the
      // timeline a few rows: the trace takes a window's height instead, and the head
      // scrolls up out of the way once, one scroll back.
      const docked = box.width < 560 && !phoneViewport && drawer instanceof HTMLElement
      el.style.height = `${Math.max(300, docked ? window.innerHeight - 92 : window.innerHeight - top - 58)}px`
      if (docked && !headFolded.current) {
        headFolded.current = true
        // Scrolled to where the agent's tabs (Details · Logs …) just stick, so the
        // head is folded above them and the trace starts right under them.
        let stuck: HTMLElement | null = drawer.querySelector<HTMLElement>('[role="tablist"]')
        while (stuck !== null && stuck !== drawer && getComputedStyle(stuck).position !== 'sticky') stuck = stuck.parentElement
        drawer.scrollTop = 0
        const port = drawer.getBoundingClientRect().top + drawer.clientTop
        drawer.scrollTop =
          stuck !== null && stuck !== drawer
            ? Math.max(0, stuck.getBoundingClientRect().top - port - (parseFloat(getComputedStyle(stuck).top) || 0) - 6)
            : Math.max(0, top - port - 8)
      }
    }
    put()
    const ro = new ResizeObserver(put)
    ro.observe(el)
    window.addEventListener('resize', put)
    return () => {
      ro.disconnect()
      window.removeEventListener('resize', put)
    }
  }, [])

  const pick = useCallback((key: string) => {
    setPicked(key)
    setPhoneDetail(true)
  }, [])
  // The picked row is scrolled into view; a live run's newest is, as it arrives.
  useEffect(() => {
    const row = list.current?.querySelector<HTMLElement>(`[data-key="${(selKey ?? '').replace(/["\\]/g, '\\$&')}"]`)
    if (row === null || row === undefined || list.current === null) return
    if (list.current.clientHeight === 0) return
    const box = list.current.getBoundingClientRect()
    const r = row.getBoundingClientRect()
    if (r.top >= box.top && r.bottom <= box.bottom) return
    // Rows are one height: the scroll lands on a row's edge, so the top row is never drawn half.
    const max = list.current.scrollHeight - list.current.clientHeight
    const want = list.current.scrollTop + r.top - box.top - box.height / 3
    list.current.scrollTop = Math.max(0, Math.min(Math.floor(max / r.height) * r.height, Math.round(want / r.height) * r.height))
  }, [selKey, events.length])

  const step = useCallback(
    (by: number) => {
      const at = shown.findIndex((e) => e.key === selKey)
      const next = shown[Math.max(0, Math.min(shown.length - 1, (at === -1 ? (by > 0 ? -1 : shown.length) : at) + by))]
      if (next) setPicked(next.key)
    },
    [shown, selKey],
  )
  const jumpIn = useCallback(
    (pool: Ev[]) => {
      if (pool.length === 0) return
      // From rest the first one; once the reader has picked an event, the next after it.
      const at = picked === null ? -1 : events.findIndex((e) => e.key === selKey)
      const next = pool.find((e) => e.n - 1 > at) ?? pool[0]!
      if (!inFilter(next, filter)) setFilter('all')
      pick(next.key)
    },
    [events, selKey, picked, filter, pick],
  )
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const t = e.target as HTMLElement | null
      if (t !== null && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) {
        if (e.key === 'Escape') t.blur()
        return
      }
      if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); step(1) }
      else if (e.key === 'k' || e.key === 'ArrowUp') { e.preventDefault(); step(-1) }
      else if (e.key === 'e') jumpIn(errors)
      else if (e.key === '/') { e.preventDefault(); search.current?.focus() }
      else if (e.key === '[' && inInspector) setTraceWide(!wide)
      else if (e.key === 'f' && live) setPicked(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [step, jumpIn, errors, wide, live, inInspector])

  const end = events.find((e) => e.kind === 'end') ?? null
  const changesBase = typeof window === 'undefined' ? null : window.location.pathname.replace(/\/logs(\/.*)?$/, '')
  const hitAt = hits.findIndex((e) => e.key === selKey)
  const selIdx = shown.findIndex((e) => e.key === selKey)
  const keptEvs = kept.map((k) => events.find((e) => e.key === k)).filter((e): e is Ev => e !== undefined && e.key !== selKey)
  const newer = sel !== null && live && !following ? events.length - sel.n : 0

  const filters: { k: Filter; label: string; key: string }[] = [
    { k: 'all', label: 'All', key: '' },
    { k: 'messages', label: layout === 'phone' ? 'Msgs' : 'Messages', key: '' },
    { k: 'tools', label: 'Tools', key: '' },
    { k: 'edits', label: 'Edits', key: '' },
    { k: 'errors', label: 'Errors', key: '' },
  ]

  const timeline = (
    <div className="trc-left">
      <ol ref={list} className="trc-list" aria-label="Events" tabIndex={0}>
        {read !== null && read.window_starts_mid_stream && (
          <li className="trc-gap">
            earlier events are not in this read
            {!live && (
              <button type="button" className="trc-link" onClick={() => onFromStart(true)}>
                Read from the first window
              </button>
            )}
          </li>
        )}
        {shown.map((e) => {
          const hit = needle !== '' && e.hay.includes(needle)
          return (
            <li key={e.key} data-key={e.key}>
              <button
                type="button"
                className={`trc-row is-${e.kind}${e.key === selKey ? ' is-sel' : ''}${e.status === 'error' ? ' is-error' : ''}${hit ? ' is-hit' : ''}${needle !== '' && !hit ? ' is-miss' : ''}`}
                onClick={() => pick(e.key)}
                aria-current={e.key === selKey ? 'true' : undefined}
                title={e.label}
              >
                <span className="trc-n">{e.n}</span>
                <Icon kind={e.kind} error={e.status === 'error'} />
                <span className={`trc-label${e.call !== null && e.kind !== 'edit' && e.call.tool?.name === 'Bash' ? ' mono' : ''}`}>
                  <span className="trc-l"><Marked text={e.label} needle={needle} /></span>
                  {e.sub && <span className="trc-sub">{e.sub}</span>}
                </span>
                {e.progress > 0 && <span className="trc-prog" title={`${e.progress} progress event${e.progress === 1 ? '' : 's'} folded into this call`}>{e.progress}</span>}
                <Secs ev={e} />
                <Status ev={e} />
              </button>
            </li>
          )
        })}
        {shown.length === 0 && read !== null && <li className="trc-gap">no {filter} in this read</li>}
        {fromStart && !live && read !== null && !read.complete && (
          <li className="trc-gap">
            <button type="button" className="trc-link" onClick={() => onFromStart(false)}>
              Read the newest window
            </button>
          </li>
        )}
        {live && <li className="trc-gap trc-tail" aria-live="polite" title={`Read again every ${POLL_MS / 1000} s`}><span className="trc-pulse" /> live</li>}
      </ol>
    </div>
  )

  const detail = (
    <div className={`trc-right${keptEvs.length > 0 ? ' has-kept' : ''}`} aria-label="Picked event">
      {layout === 'phone' && (
        <button type="button" className="trc-back" onClick={() => setPhoneDetail(false)}>← Events</button>
      )}
      {sel === null ? (
        read !== null && failed === null ? (
          <StreamStatus t={read} task={task} />
        ) : (
          <p className="trc-dim trc-pad">{failed !== null ? `Not read: ${failed}` : 'Reading the transcript…'}</p>
        )
      ) : (
        <>
          <div className="trc-slot">
          <Detail
            key={sel.key}
            ev={sel}
            trace={trace}
            needle={needle}
            lastAnswer={lastAnswer}
            onPick={pick}
            changesBase={changesBase}
            taskId={task.id}
            shots={shots}
            onKeep={layout === 'phone' ? undefined : () => setKept((k) => (k.includes(sel.key) ? k : [...k.slice(-1), sel.key]))}
            kept={kept.includes(sel.key)}
          />
          </div>
          {keptEvs.map((k) => (
            <div key={`kept-${k.key}`} className="trc-slot is-kept">
              <Detail taskId={task.id} shots={shots} ev={k} trace={trace} needle={needle} lastAnswer={lastAnswer} onPick={pick} changesBase={changesBase} kept compact onUnkeep={() => setKept((ks) => ks.filter((x) => x !== k.key))} />
            </div>
          ))}
        </>
      )}
    </div>
  )

  return (
    <section ref={root} className={`trc is-${layout}${phoneDetail && layout === 'phone' ? ' is-detail' : ''}`} aria-label="Logs" data-view="trace">
      <div className="trc-bar">
        {streams}
        {tools}
        <label className="trc-search">
          <input
            ref={search}
            type="search"
            value={query}
            placeholder="Search…"
            aria-label="Search this window"
            title="Search this window: the events this read holds, not the whole stream; matches are marked in the timeline (/)"
            onChange={(e) => onQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && hits.length > 0) {
                e.preventDefault()
                const next = hits[(hitAt + (e.shiftKey ? hits.length - 1 : 1)) % hits.length] ?? hits[0]!
                setPicked(next.key)
              }
            }}
          />
          {needle !== '' && <span className="trc-count" aria-live="polite">{hits.length === 0 ? '0' : `${hitAt < 0 ? '–' : hitAt + 1}/${hits.length}`}</span>}
        </label>
        <button
          type="button"
          className="trc-btn trc-err"
          disabled={errors.length === 0}
          onClick={() => jumpIn(errors)}
          title="Next event the CLI marked is_error (e). A failure the CLI did not mark is not counted."
        >
          {errors.length === 0 ? 'No errors' : `Jump to error · ${errors.length}`}
        </button>
        {live && (
          <button type="button" className={`trc-btn${following ? ' is-on' : ''}`} aria-pressed={following} onClick={() => setPicked(following ? (sel?.key ?? null) : null)} title="Keep the newest event picked as it arrives (f)">
            {following ? 'Following' : newer > 0 ? `↓ ${newer} newer · Follow` : 'Follow'}
          </button>
        )}
        {!phoneViewport && !wide && (
          <button type="button" className="trc-btn trc-fold" onClick={fullWidth} aria-label="Full width" title="Fold the agent list away and read the transcript full width ([)">
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden>
              <path d="M3 3v10M13 8H6M9 5L6 8l3 3" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            {layout === 'phone' ? null : ' Full width'}
          </button>
        )}
      </div>
      <div className="trc-bar2">
        <div className="trc-chips" role="group" aria-label="Show">
          {filters.map((f) => (
            <button key={f.k} type="button" className={`trc-chip${filter === f.k ? ' is-on' : ''}${f.k === 'errors' && counts.errors > 0 ? ' is-bad' : ''}`} aria-pressed={filter === f.k} onClick={() => setFilter(f.k)} disabled={f.k !== 'all' && counts[f.k] === 0}>
              {f.label} <em>{counts[f.k]}</em>
            </button>
          ))}
        </div>
        {read !== null && <ReadFacts t={read} fetchedAt={fetchedAt} />}
        <p className="trc-summary">
          {trace.model && layout !== 'phone' && <span>{trace.model}</span>}
          {end !== null && end.secs !== null && <span title="duration_ms, from the result record">{dur(end.secs.value)}</span>}
          {end !== null && typeof end.main.meta?.num_turns === 'number' && <span>{String(end.main.meta.num_turns)} turns</span>}
          {end !== null && typeof end.main.meta?.total_cost_usd === 'number' && <span title="total_cost_usd, from the result record">${(end.main.meta.total_cost_usd as number).toFixed(2)}</span>}
          {trace.cwd && layout !== 'phone' && (
            <span className="trc-cwd" title={`Commands ran in ${trace.cwd}; that prefix is dropped from every command and path here.`}>
              <span className="trc-cwd-t">
                <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden>
                  <path d="M2.5 7.5L8 3l5.5 4.5M4 6.5V13h8V6.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                </svg>{' '}
                repo
              </span>
            </span>
          )}
          {selIdx >= 0 && <span className="trc-pos" title="j / k step through the events">{selIdx + 1} of {shown.length}</span>}
        </p>
      </div>
      {task.state === 'FAILED' && task.last_error && (
        <p className="trc-taskerr" role="note" title="The task's own last_error, from the task record, not from the transcript">
          <Icon kind="end" error /> task failed · <span className="mono">{task.last_error.split('\n')[0]}</span>
        </p>
      )}
      {read !== null && <WindowNotes t={read} />}
      <div className="trc-panes">
        {timeline}
        {detail}
      </div>
    </section>
  )
}
