// VISUAL QA OF THE CONSOLE, LANE VQ-SUBMIT (part of #2391): THE SUBMIT PAGES.
//
//   VQ-026  Submit's copy printed raw state enums in capitals: the chooser's
//           "It lands READY or PARKED", the task summary's "READY or PARKED ·
//           costs nothing", the issue form's "PLANNING, then PLANNED" and "the
//           run waits PLANNED". PICKS.md: no all-caps; a state is drawn in the
//           words the rest of the console uses (ready, parked, planning,
//           planned, running).
//   VQ-027  card 2 of the workflow form labelled its fields two ways: muted
//           lower-case `.t-label` lines ("how this work gets merged", "what
//           carries work between steps", "repository url (optional)") beside
//           bold sentence-case headings ("If a step fails", "Priority"). One
//           style, sentence case, for every field in the card -- and on the
//           task form, which draws the same dispatch control.
//
// MUTATIONS, each of which turns a case red: put any of the capitalised enums
// back in SubmitChooser.tsx, Submit.tsx or IssueSubmit.tsx; give the dispatch
// legend or a `.dsp-label` back its `t-label` class, or lower-case its words;
// drop `dsp-head` from "If a step fails" or "Priority".
//
// Nothing here is credential-shaped.

import { afterEach, describe, expect, it } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { SubmitChooser } from '../SubmitChooser'
import { SubmitScreen } from '../Submit'
import { SubmitWorkflowScreen } from '../SubmitWorkflow'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WAIT = { timeout: 4000 }
const ENVS: ReadonlyArray<readonly [string, CascadeEnv]> = [['1440', { width: 1440 }], ['390', { width: 390 }]]
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

/** A state enum written as the API spells it: the thing a person must never read. */
const RAW_STATE = /\b(QUEUED|READY|PARKED|PLANNING|PLANNED|RUNNING|LEASED|DISPATCHED|STARTING)\b/

afterEach(() => {
  document.body.innerHTML = ''
})

describe('VQ-026: Submit draws a state in words, never the enum in capitals', () => {
  it('the chooser says where an agent lands in lower case', () => {
    const { container } = render(<SubmitChooser go={() => {}} />)
    expect(visible(container)).not.toMatch(RAW_STATE)
    expect(visible(container)).toContain('It lands ready or parked and costs nothing until the scheduler admits it.')
  })

  it('the task summary says "ready or parked"', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Start one agent' }, WAIT)
    const lands = [...container.querySelectorAll('.ctl-fact')].find((li) => visible(li.querySelector('b')) === 'lands as')!
    expect(visible(lands)).toMatch(/^lands as\s*ready or parked · /)
    expect(visible(container.querySelector('.sbf-send'))).not.toMatch(RAW_STATE)
  })

  it('the issue form says "planning, then planned" under both approvals, and a workspace approves', async () => {
    const { IssueSubmitScreen } = await import('../IssueSubmit')
    const { container } = render(<IssueSubmitScreen go={() => {}} />)
    await screen.findByRole('heading', { name: 'Submit from a GitHub issue', level: 1 }, WAIT)
    await screen.findByRole('radiogroup', { name: 'runner' }, WAIT)
    const lands = () =>
      [...container.querySelectorAll('.ctl-fact')].find((li) => visible(li.querySelector(':scope > b')) === 'lands as')!
    expect(visible(lands())).toMatch(/^lands as\s*planning, then planned · a planned run holds no capacity$/)
    const required = visible(container.querySelector('.in-choice-t small'))
    expect(required).not.toMatch(RAW_STATE)
    expect(required).toContain('until someone in this workspace approves')
    expect(required).not.toContain('tenant')
    expect(visible(container.querySelector('form'))).not.toMatch(RAW_STATE)

    fireEvent.click(container.querySelector<HTMLInputElement>('input[name="plan-approval"][value="auto"]')!)
    expect(visible(lands())).toMatch(/^lands as\s*planning, then planned, approved on the next tick, then running · /)
    expect(visible(container.querySelector('form'))).not.toMatch(RAW_STATE)
  })
})

/** The label of every field in the step titled `title`. */
function fieldLabels(container: HTMLElement, title: string): HTMLElement[] {
  const step = [...container.querySelectorAll<HTMLElement>('section.sbf-move')]
    .find((s) => visible(s.querySelector('.sbf-move-h')).endsWith(title))!
  return [...step.querySelectorAll<HTMLElement>('.dsp > legend, .dsp-label, #wf-on-failure, label[for="wf-priority"]')]
}

/** How a label is painted, property by property, at one width. */
function look(el: HTMLElement, env: CascadeEnv): string {
  return ['font-size', 'line-height', 'font-weight', 'color'].map((p) => `${p}=${painted(el, p, env)}`).join('; ')
}

describe('VQ-027: one label style for every field in the dispatch card', () => {
  it('the workflow form\'s card 2: five labels, sentence case, painted alike', async () => {
    const { container } = render(<SubmitWorkflowScreen />)
    await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
    const labels = fieldLabels(container as HTMLElement, 'Choose what happens to the work')
    expect(labels.map(visible)).toEqual([
      'How this work gets merged',
      'What carries work between steps',
      'Repository url (optional)',
      'If a step fails',
      'Priority',
    ])
    for (const [w, env] of ENVS) {
      const first = look(labels[0], env)
      expect(first, `${w}: the label is not painted at all`).toContain('font-weight=600')
      for (const l of labels) expect(look(l, env), `${w}: "${visible(l)}"`).toBe(first)
    }
  })

  it('the task form\'s step 3 draws the dispatch control\'s labels the same way', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Start one agent' }, WAIT)
    const labels = fieldLabels(container as HTMLElement, 'Choose what happens to the result')
    expect(labels.map(visible)).toEqual(['How this work gets merged', 'Repository url (optional)'])
    for (const [w, env] of ENVS) {
      for (const l of labels) expect(look(l, env), `${w}: "${visible(l)}"`).toBe(look(labels[0], env))
      expect(look(labels[0], env)).toContain('color=var(--text)')
    }
  })
})
