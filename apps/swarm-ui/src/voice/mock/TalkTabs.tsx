// MOCK-HOOK FILE
/**
 * MOCK-UP, direction "tabs" (?voiceprojects=tabs): Voice it › Talk after
 * Resume, and with several projects open in one session. Drawn from
 * projectsFixture.ts; no session is opened and nothing is spoken.
 *
 *   * the catch-up card at the top of the conversation (done, decided, open,
 *     plan), the part being read out marked, with Skip;
 *   * the session's projects, a strip above the conversation: the current one
 *     pressed, each with its own cards and progress; a spoken "switch to ..."
 *     moves the current one, and New project adds one mid-session;
 *   * an aside: a task that belongs to no project, answered in a card of its
 *     own while the current project stays current.
 *
 * Address parameters: `vpscene` (catchup, two-open, switched, new-mid, aside,
 * new), `vpp` (the resumed project id).
 */
import { useState } from 'react'

import { Button, Card, Segmented, Tooltip, buttonClass } from '../../components'
import { MarkIcon } from '../../marks'
import { Ring } from '../Scope'
import { InfoGlyph } from './ProjectsTabs'
import { MOCK_CARDS, MOCK_PROJECTS, mockParam, type MockCard, type MockProject, type ProjectState } from './projectsFixture'
import '../../styles/talk-pane.css'
import '../../styles/voice-projects-mock.css'

type Scene = 'catchup' | 'two-open' | 'switched' | 'new-mid' | 'aside' | 'new'

interface SessionProject {
  id: string
  title: string
  state: ProjectState
  cards: MockCard[]
  /** Just started in this session. */
  fresh?: boolean
}

/** A card's state, said by its edge and mark; its word only in the tooltip, except Needs you, which asks. */
const CARD_MEANS: Readonly<Record<Exclude<MockCard['state'], 'waiting' | 'done'>, { mark: 'running' | 'ready'; tip: string }>> = {
  working: { mark: 'running', tip: 'Working: an agent is on it now.' },
  open: { mark: 'ready', tip: 'Open: not started; nothing is asked of you yet.' },
}

function sessionProject(id: string): SessionProject {
  const p = MOCK_PROJECTS.find((x) => x.id === id) ?? MOCK_PROJECTS[0]!
  return { id: p.id, title: p.title, state: p.state, cards: MOCK_CARDS[p.id] ?? [] }
}

const PRICES: SessionProject = { id: 'vp_mock_prices', title: 'Studio price list', state: 'progress', cards: MOCK_CARDS.vp_mock_prices!, fresh: true }

interface Line {
  side: 'you' | 'agent'
  text: string
  partial?: boolean
}

function linesFor(scene: Scene, title: string): Line[] {
  switch (scene) {
    case 'catchup':
      return [
        { side: 'you', text: `Resume the ${title.toLowerCase()}.` },
        { side: 'agent', text: `Picking up the ${title.toLowerCase()}. Last time we drafted the home and menu pages and picked the photos. We decided: pickup only, the warm palette, and orders go to the kitchen inbox`, partial: true },
      ]
    case 'two-open':
      return [
        { side: 'agent', text: 'The order form agent is six minutes in. Sunday cover still needs your answer.' },
        { side: 'you', text: 'Also open the newsletter flow, I want to pick the form today.' },
        { side: 'agent', text: 'Newsletter signup flow is open beside the bakery website. We stay on the bakery until you say switch.' },
      ]
    case 'switched':
      return [
        { side: 'you', text: 'Switch to the newsletter flow.' },
        { side: 'agent', text: 'Newsletter signup flow. It is waiting on your pick: one step or two steps. The bakery order form keeps running meanwhile.' },
      ]
    case 'new-mid':
      return [
        { side: 'you', text: 'Start a new project: a price list for the studio.' },
        { side: 'agent', text: 'New project, Studio price list, started. The bakery website and the newsletter flow stay open. Which rooms go on it?' },
      ]
    case 'aside':
      return [
        { side: 'you', text: 'Before that: what is the state of my running agents?' },
        { side: 'agent', text: 'Three are running: the bakery order form, a docs link check and a nightly backup test. None is waiting on you. Back to the bakery website: Sunday cover.' },
      ]
    case 'new':
      return [{ side: 'agent', text: 'A new project. What are we making?' }]
  }
}

const CATCHUP_PARTS = [
  { key: 'done', head: 'Done' },
  { key: 'decided', head: 'Decided' },
  { key: 'open', head: 'Open' },
  { key: 'next', head: 'Plan' },
] as const

function CatchUp({ p, reading, onSkip }: { p: MockProject; reading: number; onSkip: () => void }) {
  return (
    <section className="vpt-catch" aria-label={`Catch-up: ${p.title}`} data-reading={CATCHUP_PARTS[reading]?.key}>
      <div className="vpt-catch-h">
        <h3>
          Catch-up <span className="vpt-catch-t">{p.title}</span>
        </h3>
        <span className="vpt-catch-when">Last session {p.lastWord}</span>
        <span className="vpt-reading" role="status">
          <Eq />
          Reading {CATCHUP_PARTS[reading]?.head.toLowerCase()}
        </span>
        <Button kind="secondary" size="sm" onClick={onSkip}>
          Skip
        </Button>
      </div>
      <p className="vpt-catch-goal">
        <span className="vpm-goal-h">Goal</span> {p.brief.goal}
      </p>
      <div className="vpt-catch-cols">
        {CATCHUP_PARTS.map((part, i) => {
          const items = p.brief[part.key]
          return (
            <section key={part.key} className="vpt-catch-col" data-at={i < reading ? 'read' : i === reading ? 'now' : 'later'}>
              <h4>
                {part.head} <span className="vpt-catch-n">{items.length}</span>
              </h4>
              <ol>
                {items.map((t) => (
                  <li key={t}>{t}</li>
                ))}
              </ol>
            </section>
          )
        })}
      </div>
    </section>
  )
}

function CatchUpLine({ p, onShow }: { p: MockProject; onShow: () => void }) {
  return (
    <p className="vpt-catch-line">
      <span className="vpm-goal-h">Catch-up</span> <span className="vpt-catch-t">{p.title}</span>
      <Button kind="secondary" size="sm" onClick={onShow}>
        Show
      </Button>
    </p>
  )
}

function Progress({ p }: { p: SessionProject }) {
  const done = p.cards.filter((c) => c.state === 'done').length
  const total = p.cards.length
  return (
    <span className="vpt-prog" title={`${done} of ${total} cards done`}>
      <span className="vpt-prog-bar" aria-hidden="true">
        <span style={{ width: total === 0 ? '0%' : `${Math.round((done / total) * 100)}%` }} />
      </span>
      <span className="vpt-prog-n">
        {done}/{total}
      </span>
    </span>
  )
}

function Strip({ projects, current, onPick, onNew }: { projects: SessionProject[]; current: string; onPick: (id: string) => void; onNew: () => void }) {
  return (
    <nav className="vpt-strip" aria-label="Projects in this session">
      <ul>
        {projects.map((p) => {
          const needs = p.cards.some((c) => c.state === 'waiting')
          return (
            <li key={p.id}>
              <button type="button" className="vpt-proj" aria-pressed={p.id === current} onClick={() => onPick(p.id)}>
                <span className="vpt-proj-top">
                  <span className="vpt-proj-t">{p.title}</span>
                  {p.fresh === true && (
                    <Tooltip tip="New: started in this session.">
                      <span className="vpt-new" role="img" aria-label="New in this session" tabIndex={0}>
                        <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
                          <path d="M6 1v10M1 6h10" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
                        </svg>
                      </span>
                    </Tooltip>
                  )}
                </span>
                <span className="vpt-proj-bottom">
                  <Progress p={p} />
                  {needs && <span className="vpt-needs">Needs you</span>}
                </span>
              </button>
            </li>
          )
        })}
      </ul>
      <Button kind="secondary" size="sm" onClick={onNew}>
        New project
      </Button>
      <Tooltip tip='Say "switch to …" or press a project to make it current; say "new project" to start one. A question about anything else is answered without leaving the current project.'>
        <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label="About projects in a session">
          <InfoGlyph />
        </button>
      </Tooltip>
    </nav>
  )
}

/** Equaliser bars: something is running or speaking now, not a project's state. */
function Eq({ className }: { className?: string }) {
  return (
    <span className={`vpt-eq${className === undefined ? '' : ` ${className}`}`} aria-hidden="true">
      <i />
      <i />
      <i />
    </span>
  )
}

function CardMark({ state }: { state: keyof typeof CARD_MEANS }) {
  const m = CARD_MEANS[state]
  return (
    <Tooltip tip={m.tip}>
      <span className="vpt-card-mark" data-state={state} role="img" aria-label={m.tip.split(':')[0]} tabIndex={0}>
        <MarkIcon mark={m.mark} />
      </span>
    </Tooltip>
  )
}

function CardsOf({ p }: { p: SessionProject }) {
  const live = p.cards.filter((c) => c.state !== 'done')
  const done = p.cards.filter((c) => c.state === 'done')
  return (
    <section className="vt-cards vpt-cards" aria-label={`Cards: ${p.title}`}>
      <p className="vt-cards-h">
        Cards <span className="vpt-cards-of">{p.title}</span>
        {done.length > 0 && <span className="vpt-cards-done"> · {done.length} done</span>}
      </p>
      <div className="vt-card-list">
        {live.map((c) => (
          <div key={c.title} className="vt-card" data-state={c.state}>
            <Card
              level={3}
              title={c.title}
              action={<span className="vt-card-tags">{c.state === 'waiting' ? <span className="vpt-needs">Needs you</span> : c.state !== 'done' && <CardMark state={c.state} />}</span>}
            >
              <p className="vt-card-summary">{c.line}</p>
              {c.options !== undefined && (
                <div className="vt-card-actions">
                  {c.options.map((o) => (
                    <Button key={o} size="sm">
                      {o}
                    </Button>
                  ))}
                </div>
              )}
            </Card>
          </div>
        ))}
      </div>
    </section>
  )
}

/** A task that belongs to no project: answered in the conversation, outside every project's cards. */
function Aside() {
  return (
    <section className="vpt-aside" aria-label="Answered outside any project">
      <div className="vt-card">
        <Card
          level={3}
          title="Running agents"
          action={
            <Tooltip tip="Asked in this session; belongs to no project, so no project's cards change.">
              <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label="About this answer">
                <InfoGlyph />
              </button>
            </Tooltip>
          }
        >
          <ul className="vpt-agents">
            <li>
              <Eq className="vpt-run" /> Bakery order form <span className="vpm-faint">6 min</span>
            </li>
            <li>
              <Eq className="vpt-run" /> Docs link check <span className="vpm-faint">2 min</span>
            </li>
            <li>
              <Eq className="vpt-run" /> Nightly backup test <span className="vpm-faint">14 min</span>
            </li>
          </ul>
        </Card>
      </div>
    </section>
  )
}

function MicGlyph() {
  return (
    <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false" width="16" height="16">
      <rect x="9" y="3" width="6" height="11" rx="3" fill="none" stroke="currentColor" strokeWidth="1.6" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  )
}

/** Voice it › Talk, the tabs direction's catch-up and session projects. */
export function MockTalkTabs() {
  const initial = (mockParam('vpscene') as Scene | null) ?? 'catchup'
  const resumed = mockParam('vpp') ?? 'vp_mock_bakery'
  const first = resumed === 'new' ? PRICES : sessionProject(resumed)
  const startProjects: SessionProject[] =
    initial === 'catchup' || initial === 'new'
      ? [first]
      : initial === 'new-mid'
        ? [sessionProject('vp_mock_bakery'), sessionProject('vp_mock_newsletter'), PRICES]
        : [sessionProject('vp_mock_bakery'), sessionProject('vp_mock_newsletter')]
  const [scene, setScene] = useState<Scene>(initial)
  const [projects, setProjects] = useState<SessionProject[]>(startProjects)
  const [current, setCurrent] = useState<string>(initial === 'switched' ? 'vp_mock_newsletter' : initial === 'new-mid' ? PRICES.id : startProjects[0]!.id)
  const [catchUp, setCatchUp] = useState<'shown' | 'line' | 'none'>(initial === 'catchup' ? 'shown' : initial === 'new' ? 'none' : 'line')
  const cur = projects.find((p) => p.id === current) ?? projects[0]!
  const curMock = MOCK_PROJECTS.find((p) => p.id === cur.id)
  const lines = linesFor(scene, cur.title)

  return (
    <div className="vt vpt" data-state="speaking" data-scene={scene}>
      <div className="vt-sky" aria-hidden="true" />
      <div className="vt-hud">
        <p className="vt-status" role="status">
          <span className="vpt-live" aria-hidden="true" />
          Live
        </p>
        <span className="vt-hud-actions">
          <Button kind="secondary" size="sm">
            End
          </Button>
        </span>
      </div>

      <Strip
        projects={projects}
        current={current}
        onPick={(id) => {
          setCurrent(id)
          setScene('switched')
          setCatchUp('line')
        }}
        onNew={() => {
          setProjects((ps) => (ps.some((p) => p.id === PRICES.id) ? ps : [...ps, PRICES]))
          setCurrent(PRICES.id)
          setScene('new-mid')
          setCatchUp('none')
        }}
      />

      <div className="vpt-main">
        <div className="vpt-convo">
          {curMock !== undefined && catchUp === 'shown' && <CatchUp p={curMock} reading={1} onSkip={() => setCatchUp('line')} />}
          {curMock !== undefined && catchUp === 'line' && <CatchUpLine p={curMock} onShow={() => setCatchUp('shown')} />}
          <div className="vt-subs-band">
            <div className="vt-subs">
              <div className="vt-captions" role="log" aria-label="Captions">
                {scene === 'aside' && <p className="vpt-aside-mark">Aside · {cur.title} stays current</p>}
                {scene === 'switched' && <p className="vpt-aside-mark">Switched to {cur.title}</p>}
                {scene === 'new-mid' && <p className="vpt-aside-mark">New project · {projects.length - 1} others stay open</p>}
                {lines.map((l, i) => (
                  <p key={i} className={`vt-cap is-${l.side}${l.partial === true ? ' is-partial' : ''}`}>
                    <span className="vt-cap-who">{l.side === 'you' ? 'You' : 'Voice it'}</span>
                    <span className="vt-cap-text">
                      {l.text}
                      {l.partial === true && <span className="vt-cap-more">…</span>}
                    </span>
                  </p>
                ))}
              </div>
            </div>
          </div>
          {scene === 'aside' && <Aside />}
          <CardsOf p={cur} />
        </div>
        <div className="vpt-core">
          <div className="vt-core">
            <Ring state="speaking" voice={null} mic={null} />
          </div>
          <div className="vt-mic">
            <div className="vt-mic-group" role="group" aria-label="Microphone">
              <Tooltip tip="Voice it is speaking. Talk over it to interrupt.">
                <span className="vpt-speaking" role="img" aria-label="Voice it is speaking" tabIndex={0}>
                  <Eq />
                </span>
              </Tooltip>
              <Button kind="secondary" icon={<MicGlyph />}>
                Mute
              </Button>
              <Segmented<'open' | 'ptt'>
                small
                label="How the mic listens"
                value="open"
                options={[
                  { key: 'open', label: 'Open mic' },
                  { key: 'ptt', label: 'Push to talk' },
                ]}
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
