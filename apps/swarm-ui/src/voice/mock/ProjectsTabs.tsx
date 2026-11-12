// MOCK-HOOK FILE
/**
 * MOCK-UP, direction "tabs" (?voiceprojects=tabs): Voice it › Projects as one
 * list with state tabs above it. Each row: the state mark (its meaning in the
 * tooltip), the title, the one-line next step from the brief, the last session
 * and Resume. Opening a row unfolds its brief. Drawn from projectsFixture.ts;
 * nothing here reads or writes swarm-api.
 *
 * Address parameters, for the frames: `vptab` (progress, waiting, completed,
 * cancelled, all; two joined by a comma), `vparch=1` (All's archived group open), `vpopen` (a project id whose brief is unfolded),
 * `vpmark` (a project id whose close dialog is open), `vpempty=1`.
 */
import { useId, useMemo, useState } from 'react'

import { Banner, Button, buttonClass, Dialog, EmptyState, Menu, Segmented, Tooltip } from '../../components'
import { MarkIcon, type MarkName } from '../../marks'
import { MOCK_CARDS, MOCK_PROJECTS, STATE_MEANS, STATE_WORD, mockParam, type MockProject, type ProjectState } from './projectsFixture'
import '../../styles/voice-projects-mock.css'

type TabKey = 'progress' | 'waiting' | 'completed' | 'cancelled' | 'all'

const TAB_STATES: Readonly<Record<TabKey, readonly ProjectState[]>> = {
  progress: ['progress'],
  waiting: ['waiting'],
  completed: ['completed'],
  cancelled: ['cancelled'],
  all: ['progress', 'waiting', 'completed', 'cancelled', 'archived'],
}

const TAB_LABEL: Readonly<Record<TabKey, string>> = {
  progress: 'In progress',
  waiting: 'Waiting',
  completed: 'Completed',
  cancelled: 'Cancelled',
  all: 'All',
}

/** Each state's own silhouette and hue, so no two read alike without colour. */
const STATE_MARK: Readonly<Record<ProjectState, { mark: MarkName | 'warn'; hue: string }>> = {
  progress: { mark: 'running', hue: 'live' },
  waiting: { mark: 'parked', hue: 'park' },
  completed: { mark: 'succeeded', hue: 'neu' },
  cancelled: { mark: 'cancelled', hue: 'neu' },
  archived: { mark: 'queued', hue: 'arch' },
}

/** The state mark, focusable, its meaning in its tooltip. */
export function ProjectMark({ state }: { state: ProjectState }) {
  const m = STATE_MARK[state]
  return (
    <Tooltip tip={STATE_MEANS[state]}>
      <span className="vpm-mark" data-state={state} data-hue={m.hue} tabIndex={0} role="img" aria-label={STATE_WORD[state]}>
        <MarkIcon mark={m.mark} />
      </span>
    </Tooltip>
  )
}

/** The mark alone, as decoration beside its word. */
function ProjectMarkGlyph({ state }: { state: ProjectState }) {
  const m = STATE_MARK[state]
  return (
    <span className="vpm-mark" data-state={state} data-hue={m.hue} aria-hidden="true">
      <MarkIcon mark={m.mark} />
    </span>
  )
}

/** Where Resume goes: Talk, the catch-up first. */
export function resumeMock(id: string, scene = 'catchup'): void {
  window.history.pushState(null, '', `/voice/talk?voiceprojects=tabs&vpscene=${scene}&vpp=${id}`)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

function BriefList({ head, items, none }: { head: string; items: readonly string[]; none: string }) {
  return (
    <section className="vpm-brief-col">
      <h4>{head}</h4>
      {items.length === 0 ? (
        <p className="vpm-none">{none}</p>
      ) : (
        <ul>
          {items.map((t) => (
            <li key={t}>{t}</li>
          ))}
        </ul>
      )}
    </section>
  )
}

function Brief({ p, onMark }: { p: MockProject; onMark: (p: MockProject, to: 'completed' | 'cancelled') => void }) {
  const live = p.state === 'progress' || p.state === 'waiting'
  return (
    <div className="vpm-brief" role="region" aria-label={`Brief: ${p.title}`}>
      <p className="vpm-goal">
        <span className="vpm-goal-h">Goal</span> {p.brief.goal}
      </p>
      <div className="vpm-brief-cols">
        <BriefList head="Decided" items={p.brief.decided} none="Nothing yet" />
        <BriefList head="Open" items={p.brief.open} none="Nothing open" />
        <BriefList head="Next" items={p.brief.next.filter((n) => n !== 'Nothing planned')} none="Nothing planned" />
      </div>
      <div className="vpm-brief-acts">
        <Button kind="primary" size="sm" onClick={() => resumeMock(p.id)}>
          Resume
        </Button>
        {live && (
          <>
            <Button size="sm" onClick={() => onMark(p, 'completed')}>
              Mark completed
            </Button>
            <Button size="sm" onClick={() => onMark(p, 'cancelled')}>
              Mark cancelled
            </Button>
          </>
        )}
      </div>
    </div>
  )
}

function Row({
  p,
  open,
  onToggle,
  onMark,
}: {
  p: MockProject
  open: boolean
  onToggle: () => void
  onMark: (p: MockProject, to: 'completed' | 'cancelled') => void
}) {
  const briefId = useId()
  const live = p.state === 'progress' || p.state === 'waiting'
  return (
    <li className="vpm-row" data-state={p.state} data-open={open || undefined}>
      <div className="vpm-row-main">
        <ProjectMark state={p.state} />
        <button type="button" className="vpm-title" aria-expanded={open} aria-controls={briefId} onClick={onToggle}>
          <span className="vpm-title-t">{p.title}</span>
          <span className="vpm-line">{p.line}</span>
          <svg className="vpm-chev" viewBox="0 0 12 12" aria-hidden="true" focusable="false">
            <path d="M3 4.5 6 7.5 9 4.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
        <span className="vpm-ask">{p.ask !== undefined && <span className="vpm-ask-w">{p.ask}</span>}</span>
        <span className="vpm-when">
          <span className="vpm-k">Last session</span> {p.lastWord}
        </span>
        <span className="vpm-cards" title={live ? `${p.openCards} open cards` : undefined}>
          {p.openCards > 0 ? (
            <>
              <b>{p.openCards}</b> <span className="vpm-k">open</span>
            </>
          ) : (
            live && <span className="vpm-faint">—</span>
          )}
        </span>
        <span className="vpm-acts">
          <Button kind={live ? 'primary' : 'secondary'} size="sm" onClick={() => resumeMock(p.id)}>
            Resume
          </Button>
          <Menu
            label={`More for ${p.title}`}
            trigger={
              <>
                <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false" width="16" height="16">
                  <circle cx="5" cy="12" r="1.7" fill="currentColor" />
                  <circle cx="12" cy="12" r="1.7" fill="currentColor" />
                  <circle cx="19" cy="12" r="1.7" fill="currentColor" />
                </svg>
                <span className="sk-vh">More for {p.title}</span>
              </>
            }
            items={[
              ...(live
                ? [
                    { key: 'done', label: 'Mark completed', onSelect: () => onMark(p, 'completed') },
                    { key: 'cancel', label: 'Mark cancelled', onSelect: () => onMark(p, 'cancelled') },
                  ]
                : [{ key: 'reopen', label: 'Reopen', onSelect: () => resumeMock(p.id) }]),
              { key: 'rename', label: 'Rename', divider: true },
              { key: 'delete', label: 'Delete…', danger: true },
            ]}
          />
        </span>
      </div>
      <div id={briefId} hidden={!open}>
        {open && <Brief p={p} onMark={onMark} />}
      </div>
    </li>
  )
}

function MarkDialog({
  p,
  to,
  onTo,
  onClose,
  onDone,
}: {
  p: MockProject
  to: 'completed' | 'cancelled'
  onTo: (t: 'completed' | 'cancelled') => void
  onClose: () => void
  onDone: (outcome: string) => void
}) {
  const [outcome, setOutcome] = useState(to === 'completed' ? (p.closeAs ?? '') : '')
  const cards = (MOCK_CARDS[p.id] ?? []).filter((c) => c.state !== 'done')
  return (
    <Dialog
      title={`Close ${p.title}`}
      onClose={onClose}
      actions={
        <>
          <Button kind="ghost" onClick={onClose}>
            Keep it open
          </Button>
          <Button kind={to === 'cancelled' ? 'danger' : 'primary'} onClick={() => onDone(outcome)}>
            {to === 'completed' ? 'Mark completed' : 'Mark cancelled'}
          </Button>
        </>
      }
    >
      <div className="vpm-dlg">
        <Segmented<'completed' | 'cancelled'>
          label="Close as"
          value={to}
          options={[
            { key: 'completed', label: 'Completed', title: STATE_MEANS.completed },
            { key: 'cancelled', label: 'Cancelled', title: STATE_MEANS.cancelled },
          ]}
          onChange={onTo}
        />
        <label className="c-field">
          <span className="c-lbl">{to === 'completed' ? 'Outcome' : 'Why'}</span>
          <input className="c-inp" value={outcome} maxLength={120} placeholder="One line, kept in the brief" onChange={(e) => setOutcome(e.target.value)} />
        </label>
        <dl className="vpm-dlg-facts">
          <dt>Closed with it</dt>
          <dd>
            {cards.length === 0 ? (
              'No open cards'
            ) : (
              <ul className="vpm-dlg-cards">
                {cards.map((c) => (
                  <li key={c.title}>
                    {c.title}
                    {c.state === 'working' && <b> · its running agent is stopped</b>}
                  </li>
                ))}
              </ul>
            )}
          </dd>
          <dt>Brief</dt>
          <dd>Kept; Resume reopens it</dd>
        </dl>
      </div>
    </Dialog>
  )
}

/** Voice it › Projects, the tabs direction. */
export function MockProjectsTabs() {
  const empty = mockParam('vpempty') === '1'
  const [rows, setRows] = useState<MockProject[]>(() => (empty ? [] : [...MOCK_PROJECTS]))
  // Tabs combine: Shift- or Ctrl-click a second tab to see both (`vptab=completed,cancelled`).
  const [sel, setSel] = useState<TabKey[]>(() => ((mockParam('vptab') ?? 'progress').split(',') as TabKey[]))
  const [archOpen, setArchOpen] = useState(() => mockParam('vparch') === '1')
  const [open, setOpen] = useState<string | null>(() => mockParam('vpopen'))
  const [marking, setMarking] = useState<{ p: MockProject; to: 'completed' | 'cancelled' } | null>(() => {
    const id = mockParam('vpmark')
    const p = MOCK_PROJECTS.find((x) => x.id === id)
    return p === undefined ? null : { p, to: 'completed' }
  })
  const [moved, setMoved] = useState<{ p: MockProject; was: ProjectState } | null>(null)

  const counts = useMemo(() => {
    const c = (k: TabKey) => rows.filter((r) => TAB_STATES[k].includes(r.state)).length
    return { progress: c('progress'), waiting: c('waiting'), completed: c('completed'), cancelled: c('cancelled'), all: c('all') }
  }, [rows])

  const all = sel.includes('all')
  const states = new Set(sel.flatMap((k) => TAB_STATES[k]))
  const byLast = (a: MockProject, b: MockProject) => Date.parse(b.lastAt) - Date.parse(a.lastAt)
  const shown = rows.filter((r) => states.has(r.state) && r.state !== 'archived').sort(byLast)
  const archived = all ? rows.filter((r) => r.state === 'archived').sort(byLast) : []
  const selLabel = sel.map((k) => TAB_LABEL[k]).join(' and ')
  // The row's line is the next step while open, the outcome or reason once closed: the header says which it shows.
  const anyLive = shown.some((r) => r.state === 'progress' || r.state === 'waiting')
  const anyDone = shown.some((r) => r.state === 'completed')
  const anyStopped = shown.some((r) => r.state === 'cancelled') || archived.length > 0
  const says = [anyLive && 'next step', anyDone && 'outcome', anyStopped && 'reason'].filter((w): w is string => typeof w === 'string')
  const lineHead = `Project · ${says.length > 1 ? `${says.slice(0, -1).join(', ')} or ${says[says.length - 1]}` : (says[0] ?? 'next step')}`
  const pick = (k: TabKey, add: boolean) => {
    if (k === 'all' || !add) return setSel([k])
    const next = sel.filter((x) => x !== 'all')
    const toggled = next.includes(k) ? next.filter((x) => x !== k) : [...next, k]
    setSel(toggled.length === 0 ? [k] : toggled)
  }

  if (rows.length === 0) {
    return (
      <div className="vpm">
        <EmptyState
          kind="empty"
          heading="No projects yet"
          action={
            <span className="vpm-empty-acts">
              <Button kind="primary" onClick={() => resumeMock('new', 'new')}>
                Start a project in Talk
              </Button>
              <Tooltip tip="Every talk in Voice it is kept here as a project, with its brief: what was done, decided, left open, and what comes next.">
                <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label="About projects">
                  <InfoGlyph />
                </button>
              </Tooltip>
            </span>
          }
        >
          {null}
        </EmptyState>
      </div>
    )
  }

  const mark = (p: MockProject, to: 'completed' | 'cancelled') => setMarking({ p, to })

  return (
    <div className="vpm">
      <div className="vpm-bar">
        <div className="c-tabs vpm-tabs" role="tablist" aria-label="Projects by state" aria-multiselectable="true">
          {(['progress', 'waiting', 'completed', 'cancelled', 'all'] as const).map((k) => (
            <button
              key={k}
              type="button"
              role="tab"
              aria-selected={sel.includes(k)}
              tabIndex={k === sel[0] ? 0 : -1}
              onClick={(e) => pick(k, e.shiftKey || e.ctrlKey || e.metaKey)}
            >
              <span className="c-tab-label">{TAB_LABEL[k]}</span> <em>{counts[k]}</em>
            </button>
          ))}
        </div>
        {sel.length > 1 && (
          <Button kind="ghost" size="sm" onClick={() => setSel([sel[0]!])}>
            Clear
          </Button>
        )}
        <Tooltip tip="Shift-click a second tab to see both at once, for example Completed and Cancelled.">
          <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label="About the state tabs">
            <InfoGlyph />
          </button>
        </Tooltip>
        <span className="vpm-bar-gap" />
        <Button kind="secondary" size="sm" onClick={() => resumeMock('new', 'new')}>
          New project
        </Button>
      </div>
      {moved !== null && (
        <Banner
          tone="neu"
          title={`${moved.p.title} moved to ${STATE_WORD[moved.p.state]}`}
          actions={
            <Button
              kind="ghost"
              size="sm"
              onClick={() => {
                setRows((rs) => rs.map((r) => (r.id === moved.p.id ? { ...r, state: moved.was } : r)))
                setMoved(null)
              }}
            >
              Undo
            </Button>
          }
        />
      )}
      <div className="vpm-head" aria-hidden="true">
        <span />
        <span>{lineHead}</span>
        <span />
        <span>Last session</span>
        <span>{anyLive ? 'Cards' : ''}</span>
        <span />
      </div>
      {shown.length === 0 && archived.length === 0 ? (
        <p className="vpm-none vpm-tab-none">No {selLabel.toLowerCase()} projects</p>
      ) : (
        <ul className="vpm-list" aria-label={`${selLabel} projects`}>
          {shown.map((p) => (
            <Row key={p.id} p={p} open={open === p.id} onToggle={() => setOpen((o) => (o === p.id ? null : p.id))} onMark={mark} />
          ))}
          {archived.length > 0 && (
            <li className="vpm-arch">
              <button type="button" className="vpm-arch-h" aria-expanded={archOpen} onClick={() => setArchOpen((v) => !v)}>
                <ProjectMarkGlyph state="archived" />
                Archived <em>{archived.length}</em>
                <svg className="vpm-chev" viewBox="0 0 12 12" aria-hidden="true" focusable="false">
                  <path d="M3 4.5 6 7.5 9 4.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
            </li>
          )}
          {archOpen &&
            archived.map((p) => (
              <Row key={p.id} p={p} open={open === p.id} onToggle={() => setOpen((o) => (o === p.id ? null : p.id))} onMark={mark} />
            ))}
        </ul>
      )}
      {marking !== null && (
        <MarkDialog
          p={marking.p}
          to={marking.to}
          onTo={(to) => setMarking({ ...marking, to })}
          onClose={() => setMarking(null)}
          onDone={() => {
            const was = marking.p.state
            const next: MockProject = { ...marking.p, state: marking.to, openCards: 0 }
            setRows((rs) => rs.map((r) => (r.id === next.id ? next : r)))
            setMoved({ p: next, was })
            setMarking(null)
          }}
        />
      )}
    </div>
  )
}

export function InfoGlyph() {
  return (
    <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="1.6" />
      <path d="M12 11v6M12 7.5v.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  )
}
