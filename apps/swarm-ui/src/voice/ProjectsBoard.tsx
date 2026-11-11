// MOCK-HOOK FILE
/**
 * MOCK-UP, DIRECTION "BOARD BY STATE" (?voiceprojects=board). Nothing here
 * reads or writes swarm-api: the projects, briefs, catch-ups and the session's
 * open projects are invented fixtures, because the server side (states that
 * move, the brief, the catch-up) is not built. Every frame of the direction is
 * reachable from the address so a capture opens it directly:
 *
 *   /voice/projects?voiceprojects=board[&vpb=empty][&vpf=completed,cancelled][&vpa=1][&vpo=<id>][&vpm=<id>]
 *   /voice/talk?voiceprojects=board&vpt=catchup|two-open|switched|new-mid|aside
 */
import { useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

import { Button, buttonClass, Dialog, EmptyState, Segmented, ToneMark } from '../components'
import { Ring } from './Scope'
import { TalkTip } from './Stage'
import '../styles/talk-pane.css'
import '../styles/projects-board.css'

export type PState = 'progress' | 'waiting' | 'completed' | 'cancelled' | 'archived'

const STATES: readonly PState[] = ['progress', 'waiting', 'completed', 'cancelled', 'archived']

const STATE_NAME: Record<PState, string> = {
  progress: 'In progress',
  waiting: 'Waiting',
  completed: 'Completed',
  cancelled: 'Cancelled',
  archived: 'Archived',
}

/** What each state means: only ever in its tooltip (the owner's no-prose rule). */
const STATE_TIP: Record<PState, string> = {
  progress: 'Being worked on: Voice it or its agents have open cards for it.',
  waiting: 'Stopped on you: a pick, an answer or an approval only you can give.',
  completed: 'Done: you marked it completed, or what it set out to ship has merged.',
  cancelled: 'Dropped on purpose. Its brief and cards are kept, and it can be resumed.',
  archived: 'Finished long ago and put away. Kept, resumable, out of the board.',
}

interface Brief {
  goal: string
  decided: string[]
  open: string[]
  next: string
}

interface MiniCard {
  title: string
  kind: 'mockup' | 'agent' | 'pick' | 'plan' | 'pr'
  /** Only a card that asks something carries a word. */
  asks?: string
  done?: boolean
  when: string
}

interface Project {
  id: string
  title: string
  state: PState
  next: string
  lastSession: string
  openCards: number
  /** Cards done / total: the progress line on the card. */
  done: number
  total: number
  outcome?: string
  /** What a waiting project asks of the person: the only word a state shows on the board. */
  asks?: string
  brief: Brief
  cards: MiniCard[]
}

// ---------------------------------------------------------------------------
// Fixtures: an invented small bakery's projects. No real company or person.
// ---------------------------------------------------------------------------
const FIXTURE: readonly Project[] = [
  {
    id: 'vp_mock_bakery',
    title: 'Bakery website',
    state: 'progress',
    next: 'Review the order page mock-up',
    lastSession: 'Yesterday',
    openCards: 3,
    done: 5,
    total: 8,
    brief: {
      goal: 'A five-page site for the bakery: menu, opening hours, order ahead, contact.',
      decided: ['Layout B, the photo-led home page', 'Orders collected in shop, no delivery', 'Warm palette from the shop sign'],
      open: ['Order page: one form or two steps', 'Who writes the about page'],
      next: 'Review the order page mock-up, then plan the build.',
    },
    cards: [
      { title: 'Order page mock-up', kind: 'mockup', when: 'Yesterday 17:40' },
      { title: 'Opening hours, from the shop sign', kind: 'agent', when: 'Yesterday 17:22' },
      { title: 'Build plan, first draft', kind: 'plan', when: 'Yesterday 17:05' },
      { title: 'Home page merged', kind: 'pr', done: true, when: '8 Nov' },
    ],
  },
  {
    id: 'vp_mock_newsletter',
    title: 'Newsletter signup flow',
    state: 'waiting',
    next: 'Signup form, A or B',
    asks: 'Your pick',
    lastSession: '3 days ago',
    openCards: 1,
    done: 2,
    total: 4,
    brief: {
      goal: 'A signup box on the site that sends a welcome email and a monthly letter.',
      decided: ['Double opt-in', 'Monthly, first Monday'],
      open: ['Form A (one field) or form B (name and email)'],
      next: 'Your pick between the two forms; then the agent builds it.',
    },
    cards: [
      { title: 'Signup form: A or B', kind: 'pick', asks: 'Your pick', when: '8 Nov 10:12' },
      { title: 'Welcome email draft', kind: 'mockup', done: true, when: '8 Nov 09:58' },
    ],
  },
  {
    id: 'vp_mock_menu',
    title: 'Menu app',
    state: 'completed',
    next: 'Today’s menu on the phone page',
    outcome: 'Merged #214',
    lastSession: '28 Oct',
    openCards: 0,
    done: 6,
    total: 6,
    brief: {
      goal: 'A phone page showing today’s menu, updated from one shared sheet.',
      decided: ['Reads the sheet every hour', 'Allergens shown as icons'],
      open: [],
      next: 'Nothing open. Shipped with pull request #214.',
    },
    cards: [
      { title: 'Menu app shipped', kind: 'pr', done: true, when: '28 Oct 16:03' },
      { title: 'Allergen icons', kind: 'mockup', done: true, when: '27 Oct 11:40' },
    ],
  },
  {
    id: 'vp_mock_loyalty',
    title: 'Loyalty card idea',
    state: 'cancelled',
    next: 'Paper stamp cards stay',
    lastSession: '21 Oct',
    openCards: 0,
    done: 1,
    total: 3,
    brief: {
      goal: 'A digital stamp card: ten coffees, the eleventh free.',
      decided: ['Cancelled: the paper card works and costs nothing'],
      open: [],
      next: 'Nothing. Resume it to pick it up again.',
    },
    cards: [{ title: 'Stamp card mock-up', kind: 'mockup', done: true, when: '21 Oct 09:15' }],
  },
  {
    id: 'vp_mock_invoice',
    title: 'Invoice reminder emails',
    state: 'progress',
    next: 'Agent drafting the reminder text',
    lastSession: '2 days ago',
    openCards: 2,
    done: 1,
    total: 4,
    brief: {
      goal: 'A polite email when a wholesale invoice is a week late.',
      decided: ['Sent from the shop’s address'],
      open: ['Second reminder or a phone call'],
      next: 'Read the agent’s draft.',
    },
    cards: [{ title: 'Reminder text', kind: 'agent', when: '9 Nov 14:20' }],
  },
  {
    id: 'vp_mock_rota',
    title: 'Staff rota planner',
    state: 'waiting',
    next: 'Shared calendar access',
    asks: 'Approve',
    lastSession: '5 Nov',
    openCards: 1,
    done: 2,
    total: 5,
    brief: {
      goal: 'A weekly rota on one page, with swaps by message.',
      decided: ['Week starts on Monday'],
      open: ['Calendar access, waiting on your approval'],
      next: 'Approve the calendar access.',
    },
    cards: [{ title: 'Calendar access', kind: 'pick', asks: 'Approve', when: '5 Nov 08:30' }],
  },
  {
    id: 'vp_mock_gallery',
    title: 'Photo gallery cleanup',
    state: 'completed',
    next: 'Photos load twice as fast',
    outcome: 'Merged #198',
    lastSession: '19 Oct',
    openCards: 0,
    done: 3,
    total: 3,
    brief: { goal: 'Smaller photos, the same look.', decided: ['WebP, two sizes'], open: [], next: 'Nothing open.' },
    cards: [{ title: 'Gallery images resized', kind: 'pr', done: true, when: '19 Oct' }],
  },
  {
    id: 'vp_mock_zones',
    title: 'Delivery zone map',
    state: 'completed',
    next: 'No delivery for now',
    outcome: 'Nothing to ship',
    lastSession: '14 Oct',
    openCards: 0,
    done: 2,
    total: 2,
    brief: { goal: 'Should we deliver, and how far?', decided: ['No delivery for now'], open: [], next: 'Nothing open.' },
    cards: [{ title: 'Zone sketch', kind: 'mockup', done: true, when: '14 Oct' }],
  },
  {
    id: 'vp_mock_giftcard',
    title: 'Gift card checkout',
    state: 'cancelled',
    next: 'Card fees too high',
    lastSession: '9 Oct',
    openCards: 0,
    done: 1,
    total: 4,
    brief: { goal: 'Sell gift cards online.', decided: ['Cancelled on fees'], open: [], next: 'Nothing.' },
    cards: [{ title: 'Fee comparison', kind: 'agent', done: true, when: '9 Oct' }],
  },
  {
    id: 'vp_mock_supplier',
    title: 'Supplier price sheet',
    state: 'archived',
    next: 'One weekly price sheet',
    outcome: 'Merged #151',
    lastSession: '2 Sep',
    openCards: 0,
    done: 3,
    total: 3,
    brief: { goal: 'One sheet of flour and butter prices.', decided: ['Weekly update'], open: [], next: 'Nothing.' },
    cards: [],
  },
  {
    id: 'vp_mock_hours',
    title: 'Opening hours widget',
    state: 'archived',
    next: 'Hours on every page',
    outcome: 'Merged #140',
    lastSession: '24 Aug',
    openCards: 0,
    done: 2,
    total: 2,
    brief: { goal: 'Hours on every page.', decided: ['From one file'], open: [], next: 'Nothing.' },
    cards: [],
  },
  {
    id: 'vp_mock_recipe',
    title: 'Recipe cost calculator',
    state: 'archived',
    next: 'Paused for the season',
    lastSession: '11 Aug',
    openCards: 0,
    done: 1,
    total: 3,
    brief: { goal: 'What one loaf costs to make.', decided: ['Paused for the season'], open: [], next: 'Nothing.' },
    cards: [],
  },
]

/**
 * The address's switches, kept from the first load: the console's router
 * rewrites the address to its canonical path and drops the query, so they are
 * read once and held here for the life of the page (mock only).
 */
let SWITCHES: URLSearchParams | null = null
function switches(): URLSearchParams {
  if (typeof window !== 'undefined' && new URLSearchParams(window.location.search).has('voiceprojects')) SWITCHES = new URLSearchParams(window.location.search)
  return SWITCHES ?? new URLSearchParams()
}
switches()

function param(name: string): string | null {
  return switches().get(name)
}

export function boardMockOn(): boolean {
  return typeof window !== 'undefined' && param('voiceprojects') === 'board'
}

// ---------------------------------------------------------------------------
// The state marks: a shape AND a hue each, so no state rests on colour alone.
// ---------------------------------------------------------------------------
export function StateGlyph({ state, size = 14 }: { state: PState; size?: number }) {
  const common = { width: size, height: size, viewBox: '0 0 16 16', 'aria-hidden': true as const, focusable: false as const, className: `pb-glyph is-${state}` }
  switch (state) {
    case 'progress':
      // a half-filled circle: under way
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="6.2" fill="none" stroke="currentColor" strokeWidth="1.8" />
          <path d="M8 1.8 A6.2 6.2 0 0 1 8 14.2 Z" fill="currentColor" />
        </svg>
      )
    case 'waiting':
      // a pause: stopped on you
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="6.2" fill="none" stroke="currentColor" strokeWidth="1.8" />
          <path d="M6.3 5.2v5.6M9.7 5.2v5.6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
      )
    case 'completed':
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="7" fill="currentColor" />
          <path d="M4.9 8.2l2.1 2.1 4.2-4.4" fill="none" stroke="var(--bg)" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      )
    case 'cancelled':
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="6.2" fill="none" stroke="currentColor" strokeWidth="1.8" />
          <path d="M3.8 12.2l8.4-8.4" stroke="currentColor" strokeWidth="1.8" />
        </svg>
      )
    case 'archived':
      return (
        <svg {...common}>
          <rect x="1.8" y="2.6" width="12.4" height="3.4" rx="1" fill="none" stroke="currentColor" strokeWidth="1.6" />
          <path d="M3 6v6.4a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V6M6.4 8.6h3.2" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
        </svg>
      )
  }
}

function InfoGlyph() {
  return (
    <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false" width="14" height="14">
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <path d="M12 11v6M12 7.5v.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  )
}

function CloseGlyph() {
  return (
    <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false" width="14" height="14">
      <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  )
}

const KIND_MARK: Record<MiniCard['kind'], string> = { mockup: 'Mock-up', agent: 'Agent', pick: 'Pick', plan: 'Plan', pr: 'Pull request' }

function Progress({ done, total }: { done: number; total: number }) {
  const pct = total === 0 ? 0 : Math.round((done / total) * 100)
  return (
    <span className="pb-prog" role="img" aria-label={`${done} of ${total} cards done`} title={`${done} of ${total} cards done`}>
      <span className="pb-prog-fill" style={{ width: `${pct}%` }} />
    </span>
  )
}

// ---------------------------------------------------------------------------
// Projects: the board.
// ---------------------------------------------------------------------------
function ProjectCard({ p, onOpen, picked }: { p: Project; onOpen: () => void; picked: boolean }) {
  return (
    <article
      className={`pb-card is-${p.state}${picked ? ' is-picked' : ''}`}
      aria-label={p.title}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter' && e.target === e.currentTarget) onOpen()
      }}
    >
      <h3 className="pb-card-t">
        <button type="button" className="pb-card-open" aria-expanded={picked} onClick={(e) => (e.stopPropagation(), onOpen())}>
          {p.title}
        </button>
      </h3>
      <p className="pb-card-next">
        {p.asks !== undefined && <span className="pb-ask">{p.asks}</span>}
        <span>{p.next}</span>
      </p>
      {p.outcome !== undefined && <p className="pb-card-out">{p.outcome}</p>}
      <div className="pb-card-foot">
        <span className="pb-meta" title="Last session">
          {p.lastSession}
        </span>
        {p.openCards > 0 && (
          <span className="pb-meta pb-open" title={`${p.openCards} open cards`}>
            {p.openCards} open
          </span>
        )}
        {(p.state === 'progress' || p.state === 'waiting') && <Progress done={p.done} total={p.total} />}
        <span className="pb-card-act">
          <Button size="sm" kind={p.state === 'progress' || p.state === 'waiting' ? 'primary' : 'secondary'} onClick={(e) => (e.stopPropagation(), resumeMock(p.id))}>
            Resume
          </Button>
        </span>
      </div>
    </article>
  )
}

function ColumnHead({ state, n, tipOpen }: { state: PState; n: number; tipOpen?: boolean }) {
  return (
    <h2 className={`pb-col-h is-${state}`}>
      <StateGlyph state={state} />
      <span className="pb-col-name">{STATE_NAME[state]}</span>
      <span className="pb-col-n">{n}</span>
      <span className="pb-col-i">
        <TalkTip tip={STATE_TIP[state]} end>
          <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label={`What ${STATE_NAME[state]} means`} data-tip-open={tipOpen ? '' : undefined}>
            <InfoGlyph />
          </button>
        </TalkTip>
      </span>
    </h2>
  )
}

function resumeMock(id: string): void {
  SWITCHES = new URLSearchParams({ voiceprojects: 'board', vpt: 'catchup', vpp: id })
  window.history.pushState(null, '', '/voice/talk')
  window.dispatchEvent(new PopStateEvent('popstate'))
}

function BriefPanel({ p, onClose, onMark }: { p: Project; onClose: () => void; onMark: (to: 'completed' | 'cancelled') => void }) {
  const live = p.state === 'progress' || p.state === 'waiting'
  return (
    <aside className="pb-panel" aria-label={`${p.title}, brief`}>
      <header className="pb-panel-h">
        <span className={`pb-panel-mark is-${p.state}`}>
          <StateGlyph state={p.state} size={16} />
          <span className="pb-sr">{STATE_NAME[p.state]}</span>
        </span>
        <h2>{p.title}</h2>
        <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label="Close" onClick={onClose}>
          <CloseGlyph />
        </button>
      </header>
      <div className="pb-panel-acts">
        <Button kind="primary" size="sm" onClick={() => resumeMock(p.id)}>
          Resume
        </Button>
        {live && (
          <>
            <Button size="sm" onClick={() => onMark('completed')}>
              Mark completed
            </Button>
            <Button size="sm" onClick={() => onMark('cancelled')}>
              Cancel project
            </Button>
          </>
        )}
        {!live && p.state !== 'archived' && <Button size="sm">Archive</Button>}
      </div>
      <p className="pb-panel-meta">
        Last session {p.lastSession.toLowerCase().startsWith('y') || /ago$/.test(p.lastSession) ? p.lastSession.toLowerCase() : p.lastSession}
        {p.total > 0 && (
          <>
            {' · '}
            {p.done} of {p.total} cards done
          </>
        )}
      </p>
      <section className="pb-brief" aria-label="Brief">
        <BriefRow k="Goal">{p.brief.goal}</BriefRow>
        <BriefRow k="Decided">
          <ul>
            {p.brief.decided.map((d) => (
              <li key={d}>{d}</li>
            ))}
          </ul>
        </BriefRow>
        <BriefRow k="Open">
          {p.brief.open.length === 0 ? (
            <span className="pb-none">None</span>
          ) : (
            <ul>
              {p.brief.open.map((d) => (
                <li key={d}>{d}</li>
              ))}
            </ul>
          )}
        </BriefRow>
        <BriefRow k="Next" strong>
          {p.brief.next}
        </BriefRow>
      </section>
      <section className="pb-recent" aria-label="Recent cards">
        <h3>Recent cards</h3>
        {p.cards.length === 0 ? (
          <p className="pb-none">None kept</p>
        ) : (
          <ul>
            {p.cards.map((c) => (
              <li key={c.title} className={c.done ? 'is-done' : c.asks ? 'is-asks' : ''}>
                <span className="pb-kind">{KIND_MARK[c.kind]}</span>
                <span className="pb-rc-t">{c.title}</span>
                {c.asks !== undefined && <span className="pb-ask">{c.asks}</span>}
                <span className="pb-rc-w">{c.when}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </aside>
  )
}

function BriefRow({ k, strong = false, children }: { k: string; strong?: boolean; children: ReactNode }) {
  return (
    <div className={`pb-brow${strong ? ' is-next' : ''}`}>
      <span className="pb-bk">{k}</span>
      <div className="pb-bv">{children}</div>
    </div>
  )
}

function MarkDialog({ p, to, onClose, onDone }: { p: Project; to: 'completed' | 'cancelled'; onClose: () => void; onDone: (to: 'completed' | 'cancelled') => void }) {
  const [pick, setPick] = useState<'completed' | 'cancelled'>(to)
  const [why, setWhy] = useState(to === 'completed' ? 'Home page live, merged #227' : '')
  return (
    <Dialog
      title={`Close ${p.title}`}
      onClose={onClose}
      actions={
        <>
          <Button kind="ghost" onClick={onClose}>
            Keep it open
          </Button>
          <Button kind={pick === 'cancelled' ? 'danger' : 'primary'} onClick={() => onDone(pick)}>
            {pick === 'completed' ? 'Mark completed' : 'Cancel project'}
          </Button>
        </>
      }
    >
      <div className="pb-mark">
        <Segmented<'completed' | 'cancelled'>
          label="Close as"
          value={pick}
          onChange={setPick}
          options={[
            { key: 'completed', label: 'Completed' },
            { key: 'cancelled', label: 'Cancelled' },
          ]}
        />
        <label className="c-field">
          <span className="c-lbl">{pick === 'completed' ? 'Outcome' : 'Why'}</span>
          <input className="c-inp" value={why} maxLength={120} onChange={(e) => setWhy(e.target.value)} />
        </label>
        <p className="pb-mark-n">
          {p.openCards} open cards close with it
          <TalkTip tip="Closing keeps the brief and every card. Agents it started keep running. Resume reopens it as in progress.">
            <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label="What closing does">
              <InfoGlyph />
            </button>
          </TalkTip>
        </p>
      </div>
    </Dialog>
  )
}

/** Voice it › Projects, the board direction. */
export function ProjectsBoard() {
  const empty = param('vpb') === 'empty'
  const startFilter = param('vpf')
  const [rows, setRows] = useState<Project[]>(() => (empty ? [] : FIXTURE.map((p) => ({ ...p }))))
  const [shown, setShown] = useState<PState[]>(() =>
    startFilter ? (startFilter.split(',').filter((s): s is PState => (STATES as readonly string[]).includes(s))) : ['progress', 'waiting', 'completed', 'cancelled'],
  )
  const [archived, setArchived] = useState(param('vpa') === '1')
  const [open, setOpen] = useState<string | null>(param('vpo'))
  const markId = param('vpm')
  const [marking, setMarking] = useState<{ id: string; to: 'completed' | 'cancelled' } | null>(markId ? { id: markId, to: 'completed' } : null)
  const tipOpen = param('vptip') as PState | null

  if (rows.length === 0) {
    return (
      <div className="pb">
        <EmptyState
          kind="empty"
          heading="No projects yet"
          action={
            <Button kind="primary" onClick={() => ((SWITCHES = new URLSearchParams({ voiceprojects: 'board', vpt: 'two-open' })), window.history.pushState(null, '', '/voice/talk'), window.dispatchEvent(new PopStateEvent('popstate')))}>
              Start talking
            </Button>
          }
        >
          <span className="pb-empty-states" aria-label="Where projects will sit">
            {(['progress', 'waiting', 'completed', 'cancelled'] as const).map((s) => (
              <span key={s} className={`pb-empty-col is-${s}`}>
                <StateGlyph state={s} />
                {STATE_NAME[s]}
              </span>
            ))}
            <TalkTip tip="Each talk becomes a project. It moves across these columns as work goes on, and Resume picks it up where it was left.">
              <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label="How projects arrive">
                <InfoGlyph />
              </button>
            </TalkTip>
          </span>
        </EmptyState>
      </div>
    )
  }

  const count = (s: PState) => rows.filter((p) => p.state === s).length
  const columns = STATES.filter((s) => (s === 'archived' ? archived : shown.includes(s)))
  const picked = rows.find((p) => p.id === open) ?? null
  const toggle = (s: PState) => setShown((xs) => (xs.includes(s) ? xs.filter((x) => x !== s) : [...xs, s]))
  const markP = marking ? rows.find((p) => p.id === marking.id) ?? null : null

  return (
    <div className={`pb${picked ? ' has-panel' : ''}`}>
      <div className="pb-bar" role="toolbar" aria-label="Show projects by state">
        {(['progress', 'waiting', 'completed', 'cancelled'] as const).map((s) => (
          <button key={s} type="button" className={`pb-filter is-${s}`} aria-pressed={shown.includes(s)} onClick={() => toggle(s)}>
            <StateGlyph state={s} size={12} />
            {STATE_NAME[s]}
            <span className="pb-filter-n">{count(s)}</span>
          </button>
        ))}
        <span className="pb-bar-sep" aria-hidden="true" />
        <button type="button" className="pb-filter is-archived" aria-pressed={archived} onClick={() => setArchived(!archived)}>
          <StateGlyph state="archived" size={12} />
          Archived
          <span className="pb-filter-n">{count('archived')}</span>
        </button>
        {shown.length < 4 && (
          <Button kind="ghost" size="sm" onClick={() => setShown(['progress', 'waiting', 'completed', 'cancelled'])}>
            Show all
          </Button>
        )}
      </div>
      <div className="pb-main">
        <div className="pb-board" style={{ ['--pb-cols' as string]: String(Math.max(columns.length, 1)) }}>
          {columns.map((s) => {
            const list = rows.filter((p) => p.state === s)
            return (
              <section key={s} className={`pb-col is-${s}`} aria-label={STATE_NAME[s]}>
                <ColumnHead state={s} n={list.length} tipOpen={tipOpen === s} />
                <div className="pb-col-body">
                  {list.length === 0 ? (
                    <p className="pb-none pb-col-none">None</p>
                  ) : (
                    list.map((p) => <ProjectCard key={p.id} p={p} picked={p.id === open} onOpen={() => setOpen(p.id === open ? null : p.id)} />)
                  )}
                </div>
              </section>
            )
          })}
          {columns.length === 0 && <p className="pb-none">No state picked</p>}
        </div>
        {/* On body, like the dialog, so the dialog's scrim covers the drawer too. */}
        {picked && createPortal(<BriefPanel p={picked} onClose={() => setOpen(null)} onMark={(to) => setMarking({ id: picked.id, to })} />, document.body)}
      </div>
      {marking && markP && (
        <MarkDialog
          p={markP}
          to={marking.to}
          onClose={() => setMarking(null)}
          onDone={(to) => {
            setRows((xs) => xs.map((x) => (x.id === markP.id ? { ...x, state: to, openCards: 0, next: x.brief.next } : x)))
            setMarking(null)
          }}
        />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Talk: the catch-up after Resume, and many projects in one session.
// ---------------------------------------------------------------------------
type Beat = { kind: 'session' | 'pick' | 'merge' | 'agent' | 'open'; when: string; text: string }

const CATCHUP_BEATS: readonly Beat[] = [
  { kind: 'session', when: '3 Nov', text: 'First talk: goal set, five pages' },
  { kind: 'pick', when: '4 Nov', text: 'You picked layout B, photo-led' },
  { kind: 'merge', when: '8 Nov', text: 'Home page merged, #227' },
  { kind: 'agent', when: '10 Nov', text: 'Agent read the hours off the shop sign' },
  { kind: 'session', when: 'Yesterday', text: 'Order page mock-up drawn' },
  { kind: 'open', when: 'Now', text: 'Order page: one form or two steps' },
]

const BEAT_WORD: Record<Beat['kind'], string> = { session: 'Session', pick: 'Pick', merge: 'Merge', agent: 'Agent', open: 'Open' }

/** The catch-up's four parts; the one being said is lit. */
const CATCHUP_PARTS: readonly { k: string; lines: string[] }[] = [
  { k: 'Done', lines: ['Home page live', 'Menu and hours pages built', 'Order page mock-up drawn'] },
  { k: 'Decided', lines: ['Layout B', 'Collect in shop, no delivery', 'Warm palette'] },
  { k: 'Open', lines: ['Order page: one form or two steps', 'Who writes the about page'] },
  { k: 'Plan', lines: ['Review the mock-up now', 'Agent builds the order page', 'About page last'] },
]

function CatchUp({ onSkip, narrating }: { onSkip: () => void; narrating: number }) {
  return (
    <section className="pb-cu" aria-label="Catch-up, Bakery website">
      <header className="pb-cu-h">
        <h2>
          <span className="pb-cu-k">Catch-up</span> Bakery website
        </h2>
        <span className="pb-cu-step" aria-label={`Part ${narrating + 1} of 4`}>
          {CATCHUP_PARTS.map((part, i) => (
            <span key={part.k} className={`pb-cu-dot${i < narrating ? ' is-said' : i === narrating ? ' is-now' : ''}`} />
          ))}
        </span>
        <Button size="sm" onClick={onSkip}>
          Skip catch-up
        </Button>
      </header>
      <div className="pb-cu-body">
        <ol className="pb-tl" aria-label="What happened">
          {CATCHUP_BEATS.map((b) => (
            <li key={b.text} className={`pb-tl-i is-${b.kind}`}>
              <span className="pb-tl-dot" aria-hidden="true" />
              <span className="pb-tl-w">{b.when}</span>
              <span className="pb-tl-k">{BEAT_WORD[b.kind]}</span>
              <span className="pb-tl-t">{b.text}</span>
            </li>
          ))}
        </ol>
        <div className="pb-cu-parts">
          {CATCHUP_PARTS.map((part, i) => (
            <div key={part.k} className={`pb-cu-part${i === narrating ? ' is-now' : i < narrating ? ' is-said' : ''}`} aria-current={i === narrating ? 'step' : undefined}>
              <h3>{part.k}</h3>
              <ul>
                {part.lines.map((l) => (
                  <li key={l}>{l}</li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}

interface SessionProject {
  id: string
  title: string
  state: PState
  done: number
  total: number
  open: number
  isNew?: boolean
}

const BAKERY: SessionProject = { id: 'vp_mock_bakery', title: 'Bakery website', state: 'progress', done: 5, total: 8, open: 3 }
const NEWSLETTER: SessionProject = { id: 'vp_mock_newsletter', title: 'Newsletter signup flow', state: 'waiting', done: 2, total: 4, open: 1 }
const BANNER: SessionProject = { id: 'vp_mock_banner', title: 'Holiday hours banner', state: 'progress', done: 0, total: 1, open: 1, isNew: true }

function SessionStrip({ open, current, onPick }: { open: SessionProject[]; current: string; onPick: (id: string) => void }) {
  return (
    <div className="pb-strip" role="group" aria-label="Projects open in this session">
      {open.map((p) => {
        const on = p.id === current
        return (
          <button key={p.id} type="button" className={`pb-sp is-${p.state}${on ? ' is-current' : ''}`} aria-pressed={on} onClick={() => onPick(p.id)}>
            <StateGlyph state={p.state} size={13} />
            <span className="pb-sp-t">{p.title}</span>
            {p.isNew && <span className="pb-sp-new" role="img" aria-label="Started this session" title="Started this session" />}
            {p.state === 'waiting' && !on && <span className="pb-ask">Your pick</span>}
            <Progress done={p.done} total={p.total} />
            <span className="pb-sp-n" title={`${p.open} open cards`}>
              {p.open}
            </span>
          </button>
        )
      })}
      <Button kind="secondary" size="sm">
        + New project
      </Button>
    </div>
  )
}

type Cap = { side: 'you' | 'agent'; text: string; partial?: boolean }

function Captions({ lines }: { lines: Cap[] }) {
  return (
    <div className="vt-subs">
      <div className="vt-captions" role="log" aria-label="Captions">
        {lines.map((l, i) => (
          <p key={i} className={`vt-cap is-${l.side === 'you' ? 'user' : 'agent'}${l.partial ? ' is-partial' : ''}`}>
            <span className="vt-cap-who">{l.side === 'you' ? 'You' : 'Voice it'}</span>
            <span className="vt-cap-text">
              {l.text}
              {l.partial && <span className="vt-cap-more">…</span>}
            </span>
          </p>
        ))}
      </div>
    </div>
  )
}

interface TalkCard {
  title: string
  kind: MiniCard['kind']
  body: string
  asks?: string
  actions?: string[]
}

const CARDS: Record<string, TalkCard[]> = {
  vp_mock_bakery: [
    { title: 'Order page mock-up', kind: 'mockup', body: 'Two steps: pick items, then a pickup time.', actions: ['Open'] },
    { title: 'Opening hours', kind: 'agent', body: 'Read from the shop sign photo. Tue–Sun 7:00–15:00.', actions: ['Open'] },
    { title: 'Build plan', kind: 'plan', body: 'Order page, then about page, then launch.', actions: ['Open'] },
  ],
  vp_mock_newsletter: [{ title: 'Signup form: A or B', kind: 'pick', body: 'A: email only. B: name and email.', asks: 'Your pick', actions: ['Pick A', 'Pick B'] }],
  vp_mock_banner: [{ title: 'Brief taking shape', kind: 'plan', body: 'Goal: a banner with the holiday hours, on every page. Dates not set yet.' }],
}

function CardsArea({ projectId, title }: { projectId: string; title: string }) {
  const cards = CARDS[projectId] ?? []
  return (
    <section className="vt-cards pb-cards" aria-label={`Cards, ${title}`}>
      <p className="vt-cards-h">Cards · {title}</p>
      <div className="vt-card-list">
        {cards.map((c) => (
          <div key={c.title} className="vt-card" data-state={c.asks ? 'waiting' : 'open'}>
            <div className="c-card">
              <div className="c-card-h">
                <h3>{c.title}</h3>
                <span className="vt-card-tags">
                  {c.asks ? <span className="pb-ask">{c.asks}</span> : <span className="c-tag">{KIND_MARK[c.kind]}</span>}
                </span>
              </div>
              <p className="vt-card-summary">{c.body}</p>
              {c.actions && (
                <span className="ob-acts">
                  {c.actions.map((a, i) => (
                    <Button key={a} size="sm" kind={i === 0 && c.asks ? 'primary' : 'secondary'}>
                      {a}
                    </Button>
                  ))}
                </span>
              )}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}

function Aside() {
  return (
    <section className="pb-aside" aria-label="Aside, not in a project">
      <header className="pb-aside-h">
        <span className="pb-aside-k">Aside</span>
        <h2>Your running agents</h2>
        <TalkTip end tip="Answered outside any project: nothing is added to the bakery website's brief or cards.">
          <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label="Why an aside">
            <InfoGlyph />
          </button>
        </TalkTip>
      </header>
      <table className="pb-aside-t">
        <tbody>
          <tr>
            <td>
              <ToneMark tone="live" /> Reminder text draft
            </td>
            <td>Invoice reminder emails</td>
            <td>40 min</td>
          </tr>
          <tr>
            <td>
              <ToneMark tone="paused" /> Calendar sync
            </td>
            <td>Staff rota planner</td>
            <td>
              <span className="pb-ask">Approve</span>
            </td>
          </tr>
        </tbody>
      </table>
      <Button size="sm">Dismiss</Button>
    </section>
  )
}

/** Voice it › Talk, the board direction's session (mock). */
export function BoardTalk() {
  const view = param('vpt') ?? 'two-open'
  const [skipped, setSkipped] = useState(false)
  const [current, setCurrent] = useState(view === 'switched' ? NEWSLETTER.id : view === 'new-mid' ? BANNER.id : BAKERY.id)
  const catchup = view === 'catchup' && !skipped
  const open: SessionProject[] = view === 'catchup' ? [BAKERY] : view === 'new-mid' ? [BAKERY, NEWSLETTER, BANNER] : [BAKERY, NEWSLETTER]
  const cur = open.find((p) => p.id === current) ?? BAKERY

  const caps: Record<string, Cap[]> = {
    catchup: [
      { side: 'you', text: 'Pick up the bakery website.' },
      { side: 'agent', text: 'Here is where we are. The home page is live and the menu and hours pages are built. You chose layout B, collection in shop, and the warm palette', partial: true },
    ],
    'two-open': [
      { side: 'you', text: 'Also open the newsletter flow, I want to answer that pick today.' },
      { side: 'agent', text: 'Opened beside the bakery website. We are still on the bakery order page.' },
    ],
    switched: [
      { side: 'you', text: 'Switch to the newsletter flow.' },
      { side: 'agent', text: 'Newsletter signup flow. One thing waits on you: form A, email only, or form B, name and email.' },
    ],
    'new-mid': [
      { side: 'you', text: 'New project: a banner with our holiday hours.' },
      { side: 'agent', text: 'Started “Holiday hours banner”. The bakery website stays open. Which dates should it show?' },
    ],
    aside: [
      { side: 'you', text: 'Quick one, what is the state of my running agents?' },
      { side: 'agent', text: 'Two: the invoice reminder draft is running, and the rota calendar waits on your approval. Still on the bakery website.' },
    ],
  }
  return (
    <div className={`vt pb-talk${catchup ? ' is-catchup' : ''}`} data-state="speaking">
      <div className="vt-sky" aria-hidden="true" />
      <div className="vt-hud">
        <p className="vt-status" role="status">
          <ToneMark tone="live" />
          Live
        </p>
        <span className="vt-hud-actions">
          <Button kind="secondary" size="sm">
            End
          </Button>
          <span className="pb-strip-i">
            <TalkTip end tip="Say “switch to …” to change project, “new project …” to start one. A question about anything else is answered without leaving the current project.">
              <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label="Talking across projects">
                <InfoGlyph />
              </button>
            </TalkTip>
          </span>
        </span>
      </div>
      <SessionStrip open={open} current={cur.id} onPick={setCurrent} />
      <div className="pb-stage">
        {catchup ? (
          <CatchUp onSkip={() => setSkipped(true)} narrating={1} />
        ) : view === 'aside' ? (
          <Aside />
        ) : (
          <CardsArea projectId={cur.id} title={cur.title} />
        )}
      </div>
      <div className="vt-presence" aria-label="Voice it">
        <div className="vt-core pb-core">
          <Ring state="speaking" voice={null} mic={null} />
        </div>
        <div className="vt-subs-band">
          <Captions lines={skipped ? [{ side: 'agent', text: 'Skipped. The order page mock-up is up: one form or two steps?' }] : (caps[view] ?? [])} />
        </div>
      </div>
      <div className="vt-mic">
        <div className="vt-mic-group" role="group" aria-label="Microphone">
          <Button kind="secondary">Mute</Button>
          <Segmented<'open' | 'ptt'>
            small
            label="How the mic listens"
            value="open"
            options={[
              { key: 'open', label: 'Open mic' },
              { key: 'ptt', label: 'Push to talk' },
            ]}
          />
          <span className="vt-mic-state">listening</span>
        </div>
      </div>
      {(catchup || view === 'aside') && <CardsArea projectId={BAKERY.id} title={BAKERY.title} />}
    </div>
  )
}
