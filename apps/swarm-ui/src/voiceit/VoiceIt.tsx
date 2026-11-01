// MOCK-HOOK FILE
/**
 * VOICE IT, THE PAGE (mock-ups only, lane VOICE-IT-MOCKUPS). Served at /voice
 * and driven by URL switches, all local:
 *
 *   ?vi=base|a|b|c|d|e   the direction (layout); `base` is this scaffold's own
 *                        plain layout, and a direction not built yet falls
 *                        back to it and says so
 *   ?vs=<scenario>       new-app | rescue | feature | approvals | flex
 *   ?vst=<stage>         rest | talk | cards | decide | result
 *
 * A direction is a component `(props: LayoutProps) => JSX.Element` added to
 * `DIRECTIONS`; it arranges the shared parts (parts.tsx, cards.tsx,
 * Diagram.tsx) around one `Frame` from fixtures.ts and never reads anything
 * else.
 */
import type { ReactNode } from 'react'

import { ButtonLink, Segmented } from '../components'
import { VoiceCardView } from './cards'
import {
  frameFor,
  SCENARIO_LABEL,
  SCENARIOS,
  STAGE_LABEL,
  STAGES,
  type Frame,
  type Scenario,
  type Stage,
} from './fixtures'
import { BudgetMeter, Captions, MicControls, NeedsYou, Oscilloscope, Preferences, ProjectHeader } from './parts'
import './voiceit.css'
import { PresenceLayout } from './layouts/E' // MOCK-HOOK

export const DIRECTION_KEYS = ['base', 'a', 'b', 'c', 'd', 'e'] as const
export type Direction = (typeof DIRECTION_KEYS)[number]

export interface LayoutProps {
  frame: Frame
}

export interface VoiceParams {
  vi: Direction
  vs: Scenario
  vst: Stage
}

/** The three switches, read from a query string; anything unknown is the default. */
export function readParams(search: string): VoiceParams {
  const q = new URLSearchParams(search)
  const vi = q.get('vi')
  const vs = q.get('vs')
  const vst = q.get('vst')
  return {
    vi: (DIRECTION_KEYS as readonly string[]).includes(vi ?? '') ? (vi as Direction) : 'base',
    vs: (SCENARIOS as readonly string[]).includes(vs ?? '') ? (vs as Scenario) : 'new-app',
    vst: (STAGES as readonly string[]).includes(vst ?? '') ? (vst as Stage) : 'rest',
  }
}

/** The address of a frame, keeping any other query (the theme) as it is. */
export function voiceHref(p: VoiceParams, search = ''): string {
  const q = new URLSearchParams(search)
  q.set('vi', p.vi)
  q.set('vs', p.vs)
  q.set('vst', p.vst)
  return `/voice?${q.toString()}`
}

/** The "Talk it through" entry a failed task, a workflow or an inbox item carries. */
export function TalkItThrough({ scenario = 'rescue' }: { scenario?: Scenario }) {
  return (
    <ButtonLink kind="secondary" size="sm" href={voiceHref({ vi: 'base', vs: scenario, vst: 'rest' })}>
      Talk it through
    </ButtonLink>
  )
}

// ---------------------------------------------------------------------------
// The base layout: every piece once, in a plain arrangement
// ---------------------------------------------------------------------------

export function CardList({ frame, className }: { frame: Frame; className?: string }) {
  if (frame.cards.length === 0) {
    return <p className="vi-cards-empty">No cards yet. They appear here as you talk.</p>
  }
  return (
    <div className={`vi-cards${className ? ` ${className}` : ''}`}>
      {frame.cards.map((c) => (
        <VoiceCardView key={c.id} card={c} />
      ))}
    </div>
  )
}

export function BaseLayout({ frame }: LayoutProps) {
  return (
    <div className="vi-base">
      <ProjectHeader project={frame.project} />
      <div className="vi-base-grid">
        <div className="vi-base-main">
          <section className="vi-base-voice" aria-label="Voice">
            <Oscilloscope scope={frame.scope} micLevel={frame.mic.level} height={64} />
            <MicControls mic={frame.mic} />
          </section>
          <section className="vi-base-cards" aria-label="Cards">
            <h2 className="vi-section-h">Cards</h2>
            <CardList frame={frame} />
          </section>
        </div>
        <aside className="vi-base-side" aria-label="Conversation and status">
          <NeedsYou waiting={frame.waiting} />
          <BudgetMeter budget={frame.budget} />
          <section className="vi-base-talk" aria-label="Captions">
            <h2 className="vi-section-h">Captions</h2>
            <Captions lines={frame.captions} className="vi-base-captions" />
          </section>
          <Preferences items={frame.preferences} />
        </aside>
      </div>
    </div>
  )
}

export const DIRECTIONS: Partial<Record<Direction, (p: LayoutProps) => JSX.Element>> = {
  base: BaseLayout,
  e: PresenceLayout, // MOCK-HOOK
}

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

/** The mock-up's own switches, so every frame is one click away. */
function MockSwitches({ p, search }: { p: VoiceParams; search: string }) {
  const link = (over: Partial<VoiceParams>) => voiceHref({ ...p, ...over }, search)
  return (
    <nav className="vi-switches" aria-label="Mock-up frames">
      <span className="vi-switches-l">Mock-up</span>
      <Segmented small label="Layout" value={p.vi} options={DIRECTION_KEYS.map((k) => ({ key: k, label: k === 'base' ? 'Plain' : k.toUpperCase(), href: link({ vi: k }) }))} />
      <Segmented small label="Scenario" value={p.vs} options={SCENARIOS.map((k) => ({ key: k, label: k, title: SCENARIO_LABEL[k], href: link({ vs: k }) }))} />
      <Segmented small label="Moment" value={p.vst} options={STAGES.map((k) => ({ key: k, label: STAGE_LABEL[k], href: link({ vst: k }) }))} />
    </nav>
  )
}

export function VoiceIt({ search = typeof window === 'undefined' ? '' : window.location.search }: { search?: string }) {
  const p = readParams(search)
  const frame = frameFor(p.vs, p.vst)
  const Layout = DIRECTIONS[p.vi]
  let body: ReactNode
  if (Layout === undefined) {
    body = (
      <>
        <p className="vi-fallback">Layout {p.vi.toUpperCase()} is not built yet; this is the plain layout.</p>
        <BaseLayout frame={frame} />
      </>
    )
  } else {
    body = <Layout frame={frame} />
  }
  return (
    <div className="vi-page" data-vi={p.vi} data-vs={p.vs} data-vst={p.vst}>
      <MockSwitches p={p} search={search} />
      {body}
    </div>
  )
}
