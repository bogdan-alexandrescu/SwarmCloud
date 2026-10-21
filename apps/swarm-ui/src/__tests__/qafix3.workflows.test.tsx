// QAFIX3-WORKFLOWS, THE WORKFLOW GRAPH'S CHROME (part of #1680; visual QA run 3,
// wf_d598fa7bffdb41fc9d76, 2026-10-21, the fixture build of main at 51d99c53,
// flow workflow-graph):
//
//   light-002, dark-002  the zoom switch 'Auto / Figures / Details / Names'
//          had no label on the screen: its only name was an aria-label, so a
//          sighted reader saw four words and no question they answer. It now
//          has a visible label, and the group's accessible name is that label.
//   light-003  an open stage's disclosure ended in a bare 'close ▾', which
//          reads as closing the panel or the workflow. It says what it does
//          to what: 'collapse stage'.
//
// Every assertion names the mutation that turns it red.

import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { useState } from 'react'

import { WorkflowCard } from '../Workflows'
import type { ZoomTier } from '../dag'
import type { Task, Workflow, WorkflowStep } from '../types'

const T0 = Date.parse('2026-09-23T12:00:00.000Z')
const iso = (offsetMs: number) => new Date(T0 + offsetMs).toISOString()

function step(step_id: string, depends_on: string[], over: Partial<WorkflowStep> = {}): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id: null, ...over }
}

/** One `plan`, a fan of `n` steps after it, one `report` after them: a stage too wide to draw whole. */
function wideStage(n: number): Workflow {
  const fan = Array.from({ length: n }, (_, i) => `scan-${i}`)
  const steps = [step('plan', []), ...fan.map((id) => step(id, ['plan'], { runner_profile: 'codex' })), step('report', fan)]
  return {
    workflow_id: 'wf_wide',
    state: 'RUNNING',
    tenant_id: 'u-bogdan',
    stored_state: 'RUNNING',
    state_source: 'derived',
    rollup: {
      state: 'RUNNING',
      complete: true,
      reason: 'steps_hold_capacity',
      counts: { unstarted: steps.length },
      unreadable_steps: [],
      unstarted_steps: [],
      steps_read: steps.length,
    },
    created_at: iso(-900_000),
    updated_at: iso(-60_000),
    submitted_by: 'alice@example.com',
    priority: 0,
    on_step_failure: 'FAIL_WORKFLOW',
    cancel_requested: false,
    steps,
  } as Workflow
}

/** The card with real stores behind its stage expansion and its zoom, as the screen holds them. */
function Card({ workflow }: { workflow: Workflow }) {
  const [stages, setStages] = useState<Record<string, boolean>>({})
  const [zoom, setZoom] = useState<'auto' | ZoomTier>('auto')
  return (
    <WorkflowCard
      workflow={workflow}
      taskById={new Map<string, Task>()}
      zoom={zoom}
      onZoom={(_id, next) => setZoom(next)}
      usage={{ kind: 'ready', usage: null }}
      openStages={stages}
      onToggleStage={(key, was) => setStages((s) => ({ ...s, [key]: !was }))}
      reload={() => {}}
    />
  )
}

afterEach(cleanup)

describe('light-002 / dark-002: the zoom switch is labelled on the screen', () => {
  it('shows a label before the four segments, and the group is named by it', () => {
    render(<Card workflow={wideStage(13)} />)
    // MUTATION: drop the visible label (the aria-label alone, as before) and
    // no group carries this name: the query throws.
    const seg = screen.getByRole('group', { name: 'Each step shows' })
    expect(within(seg).getAllByRole('button').map((b) => b.textContent)).toEqual(['Auto', 'Figures', 'Details', 'Names'])
    const label = seg.previousElementSibling as HTMLElement | null
    // MUTATION: keep the words only in the aria-label and this is red: no
    // element before the segments says them.
    expect(label?.textContent).toBe('Each step shows')
    // MUTATION: put the label inside the group, and a screen reader hears it
    // twice (the name, then the first item).
    expect(seg.contains(label)).toBe(false)
    // The label is chrome text, not a control.
    expect(label?.tagName).not.toBe('BUTTON')
    // MUTATION: name the group anything but the visible words and this is red.
    expect(seg.getAttribute('aria-label')).toBe(label?.textContent)
  })

  it('still switches the zoom by its segments', () => {
    render(<Card workflow={wideStage(13)} />)
    const seg = screen.getByRole('group', { name: 'Each step shows' })
    fireEvent.click(within(seg).getByRole('button', { name: 'Names' }))
    // MUTATION: drop the onChange wiring and the pressed segment stays Auto.
    expect(within(seg).getByRole('button', { name: 'Names' }).getAttribute('aria-pressed')).toBe('true')
    expect(within(seg).getByRole('button', { name: 'Auto' }).getAttribute('aria-pressed')).toBe('false')
  })
})

describe('light-003: an open stage’s disclosure says what it does', () => {
  it('reads "collapse stage", not a bare "close", once the stage is open', () => {
    const { container } = render(<Card workflow={wideStage(13)} />)
    const toggle = () => container.querySelector<HTMLButtonElement>('.wf-band > .wf-band-toggle')!
    expect(toggle().querySelector('.wf-caret')!.textContent).toBe('show all 13 ▸')
    fireEvent.click(toggle())
    expect(toggle().getAttribute('aria-expanded')).toBe('true')
    const caret = toggle().querySelector('.wf-caret')!
    // MUTATION: put back `'close'` and this reads 'close ▾', the bare word the
    // run-3 operator could not place.
    expect(caret.textContent).toBe('collapse stage ▾')
    expect(caret.textContent).not.toMatch(/^close\b/)
    // The accessible name says the same verb, so the words seen are the words heard.
    // MUTATION: word the name's last sentence without 'collapse' and this is red.
    expect(toggle().getAttribute('aria-label')).toMatch(/collapse this stage/)
    // The glyph is decoration; the word is the name.
    expect(caret.querySelector('[aria-hidden]')!.textContent).toBe('▾')
  })
})
