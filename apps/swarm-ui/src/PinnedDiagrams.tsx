/**
 * THE "PINNED TO THE PLAN" STRIP (docs/design/plans-pipeline.md §2.5, §6.2,
 * §6.4, §6.5; lane U8 DIAGRAMS-ON-PAGE; `DC:D1a`, `DC:D1b`, `DC:D6c`,
 * `DC:D7a`, `DC:D7b`).
 *
 * WHAT IT DRAWS. One card above the document holding the revision's
 * `diagrams[]` and nothing else: one diagram drawn at a time, every one named
 * on a tab with its title and what it explains (the plan, a step, a
 * requirement or an overlap), Open large, and a count naming the revision
 * whose digest covers them. Every plan diagram is drawn here and only here:
 * the thread's messages that name one link to its tab (`DiagramLink`). A
 * revision with no diagram has no strip.
 *
 * WHICH REVISION. The one on screen, until Pin to the plan on this page
 * returns revision N+1 (`plan:add-diagram`, §6.5): from then the strip draws
 * N+1's set, so the pinned diagram is on it, while the document and the bar
 * keep the revision they name until it is read again.
 *
 * THIS RUN'S SETTING (§6.4, read from `run.plan_settings`, never widened).
 * Off: no strip, no Ask for a diagram, and one line in the strip's place
 * naming the level that turned it off, with a link to that setting for
 * someone who may change it. A run planned before the field: "planned before
 * diagrams". On: nothing more is said; on is the default. A run read with no
 * `plan_settings` key at all (an older server) says nothing either.
 *
 * DIAGRAM TEXT IS DATA. Titles, reasons and sources are React text children;
 * the source is drawn only through DiagramView, which refuses and sanitises
 * it (§6.6).
 */

import { useState } from 'react'
import { Button, Dialog } from './components'
import DiagramView from './DiagramView'
import { route, write, type Result } from './fetch'
import { InfoTip } from './InfoTip'
import type { Conversation } from './PlanThread'
import { useRoleGate } from './roleGate'
import './styles/pinned-diagrams.css'

type Json = Record<string, unknown>

const text = (v: unknown): string | null => (typeof v === 'string' && v.trim() !== '' ? v.trim() : null)
const objs = (v: unknown): Json[] => (Array.isArray(v) ? v.filter((x): x is Json => x !== null && typeof x === 'object' && !Array.isArray(x)) : [])
const obj = (v: unknown): Json | null => (v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Json) : null)

/** A plan diagram as the page draws it (§6.2). */
export interface PlanDiagram {
  id: string
  kind: string | null
  title: string
  source: string
  why: string | null
  explains: { part: string; ref: string | null } | null
}

/** A diagram as served (a plan's, or a thread entry's), or null when it lacks what is drawn. */
export function diagramOf(v: unknown): PlanDiagram | null {
  const o = obj(v)
  const id = text(o?.diagram_id)
  const title = text(o?.title)
  const source = typeof o?.source === 'string' ? o.source : null
  if (o === null || id === null || title === null || source === null) return null
  const ex = obj(o.explains)
  const part = text(ex?.part)
  const ref = ex?.ref
  return {
    id,
    kind: text(o.kind),
    title,
    source,
    why: text(o.why),
    explains: part === null ? null : { part, ref: typeof ref === 'number' ? String(ref) : text(ref) },
  }
}

/** A plan's diagrams, in its order. */
export const diagramsOf = (plan: Json | null): PlanDiagram[] => (plan === null ? [] : objs(plan.diagrams).flatMap((d) => diagramOf(d) ?? []))

/** One diagram is another when it draws the same thing: `plan:add-diagram` may renumber its id (§6.5). */
export const sameDiagram = (a: PlanDiagram, b: PlanDiagram): boolean => a.kind === b.kind && a.title === b.title && a.source === b.source

/** The strip tab's element id for a diagram: what a message's link points at. */
export const tabId = (diagramId: string): string => `pp-tab-${diagramId}`

/** What a diagram explains, in a word or two: the plan, `Step N`, `RN`, or the overlap it names. */
export function explainsWord(d: PlanDiagram, plan: Json | null): string {
  const ex = d.explains
  if (ex === null || ex.part === 'plan' || ex.ref === null) return 'Plan'
  if (ex.part === 'step') {
    const at = objs(plan?.steps).findIndex((s, i) => (text(s.step_id) ?? `step-${i + 1}`) === ex.ref)
    return at === -1 ? ex.ref : `Step ${at + 1}`
  }
  if (ex.part === 'requirement') return `R${ex.ref}`
  return ex.ref
}

/** This run's diagram setting (§6.4): on, off at a level, planned before the field, or not served. */
export type DiagramSetting = { state: 'on' } | { state: 'off'; from: string } | { state: 'before' } | { state: 'unknown' }

export function diagramSetting(run: Json | null): DiagramSetting {
  if (run === null || !('plan_settings' in run)) return { state: 'unknown' }
  const s = obj(run.plan_settings)
  if (s === null || typeof s.diagrams !== 'boolean') return { state: 'before' }
  return s.diagrams ? { state: 'on' } : { state: 'off', from: text(s.from) ?? 'workspace' }
}

// ---------------------------------------------------------------------------
// The two routes (§6.5). Nothing but the digest, the anchor and the words is
// sent: the agent's shape is the platform's (invariant 10).
// ---------------------------------------------------------------------------

export interface DiagramAnchor {
  part: 'plan' | 'step' | 'requirement' | 'overlap'
  ref: string | null
}

/** Ask for a diagram of one part of the revision on screen: 202 with the pending `diagram` entry. */
export async function askForDiagram(runId: string, planDigest: string, anchor: DiagramAnchor, ask: string): Promise<Result<{ entry?: Json }>> {
  return write(route('/v1/runs/{id}/plan:diagram', { id: runId }), 'POST', { plan_digest: planDigest, anchor, ask }) as Promise<Result<{ entry?: Json }>>
}

/** Add a thread entry's diagram to the current revision: revision N+1, by the person pinning. */
export async function pinDiagram(runId: string, planDigest: string, entryId: string): Promise<Result<{ run?: Json }>> {
  return write(route('/v1/runs/{id}/plan:add-diagram', { id: runId }), 'POST', { plan_digest: planDigest, from: { entry_id: entryId } }) as Promise<Result<{ run?: Json }>>
}

// ---------------------------------------------------------------------------
// A link from a message to a diagram's tab
// ---------------------------------------------------------------------------

/** A message's name for a diagram on the strip: a link to its tab, which shows it there. */
export function DiagramLink({ d, c }: Readonly<{ d: PlanDiagram; c: Conversation }>) {
  return (
    <a
      className="pp-link"
      href={`#${tabId(d.id)}`}
      onClick={(e) => {
        e.preventDefault()
        c.showTab(d.id)
        document.getElementById(tabId(d.id))?.scrollIntoView?.({ block: 'nearest' })
      }}
    >
      {d.title}
    </a>
  )
}

// ---------------------------------------------------------------------------
// The strip, or the line in its place
// ---------------------------------------------------------------------------

const LEVEL: Record<string, string> = { schedule: 'this schedule', workspace: 'this workspace', platform: 'the platform' }

/** The setting's page: the workspace's and a schedule's are on Automate › Settings, the platform's on Admin › Settings. */
const settingHref = (from: string) => (from === 'platform' ? '/admin/limits' : '/automate/settings')

function OffLine({ from }: Readonly<{ from: string }>) {
  // Who may change it: a workspace admin for the workspace's and a schedule's; the platform's is an operator's.
  const gate = useRoleGate('admin', 'change it')
  const level = LEVEL[from] ?? from
  return (
    <p className="pp-off" data-testid="pp-off" data-from={from}>
      <span className="pp-chip">Diagrams off</span>
      <span className="pd-dim">{level}</span>
      <InfoTip about="diagrams off" tip={`Diagrams in plans were off for ${level} when this run was planned: the planner drew none, and none is asked for or pinned on this page.`} />
      {gate === null && from !== 'platform' && (
        <a className="pp-link" href={settingHref(from)} aria-label="Diagrams in plans setting">
          Settings
        </a>
      )}
    </p>
  )
}

/**
 * The strip for the revision on screen (`plan`, `shownRevision`), or -- once
 * Pin to the plan answered on this page -- for the revision it made.
 */
export function PinnedDiagrams({ plan, run, c, shownRevision }: Readonly<{ plan: Json; run: Json | null; c: Conversation; shownRevision: number | null }>) {
  const [large, setLarge] = useState(false)
  const setting = diagramSetting(run)
  const pinned = c.pinned
  const shownPlan = obj(pinned?.plan) ?? plan
  const revision = pinned !== null && typeof pinned.plan_revision === 'number' ? pinned.plan_revision : shownRevision
  const diagrams = diagramsOf(shownPlan)
  if (diagrams.length === 0) {
    if (setting.state === 'off') return <OffLine from={setting.from} />
    if (setting.state === 'before')
      return (
        <p className="pp-off" data-testid="pp-before">
          <span className="pd-dim">planned before diagrams</span>
        </p>
      )
    return null
  }
  const shown = diagrams.find((d) => d.id === c.tab) ?? diagrams[0]!
  const n = diagrams.length
  const inRevision = revision === null ? 'this revision' : `revision ${revision}`
  return (
    <section className="pp-strip" aria-label="Pinned to the plan" data-testid="pp-strip" data-count={n} data-revision={revision ?? undefined}>
      <header className="pp-head">
        <b className="pp-title">Pinned</b>
        <span className="pp-chip" data-testid="pp-count">
          {n}
          {revision !== null && ` · r${revision}`}
        </span>
        <InfoTip about="the pinned diagrams" tip={`${n} diagram${n === 1 ? '' : 's'} in ${inRevision}, inside its digest: Approve approves them with the plan.`} />
        <Button kind="ghost" size="sm" className="pp-large" onClick={() => setLarge(true)}>
          Open large
        </Button>
      </header>
      <div className="pp-tabs" role="tablist" aria-label="Pinned diagrams">
        {diagrams.map((d) => {
          const on = d.id === shown.id
          return (
            <button
              key={d.id}
              type="button"
              role="tab"
              id={tabId(d.id)}
              className={`pp-tab${on ? ' is-on' : ''}`}
              aria-selected={on}
              aria-controls="pp-panel"
              tabIndex={on ? 0 : -1}
              onClick={() => c.showTab(d.id)}
            >
              <span className="pp-tab-t">{d.title}</span>
              <span className="pp-tab-x">{explainsWord(d, shownPlan)}</span>
            </button>
          )
        })}
      </div>
      <div className="pp-draw" role="tabpanel" id="pp-panel" aria-labelledby={tabId(shown.id)}>
        <DiagramView key={shown.id} source={shown.source} title={shown.title} why={shown.why ?? undefined} size="strip" />
      </div>
      {large && (
        <Dialog
          title={shown.title}
          onClose={() => setLarge(false)}
          actions={
            <Button kind="secondary" onClick={() => setLarge(false)}>
              Close
            </Button>
          }
        >
          <DiagramView source={shown.source} title={shown.title} why={shown.why ?? undefined} size="large" />
        </Dialog>
      )}
    </section>
  )
}
