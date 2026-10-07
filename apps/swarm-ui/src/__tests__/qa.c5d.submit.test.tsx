// QA PASS 2026-10-07 ON THE SUBMIT PAGES (wave 16, lane C5D), one case per
// finding:
//
//   G1-05 / G4-23  the chooser reads your recent submissions, so its head
//                  carries that read's age on a refresh control -- never
//                  "reads nothing" beside "Reading your recent submissions…";
//   G4-24          the workflow rail says WHY a priority is refused: out of
//                  range for 500, not a whole number for 2.5;
//   G4-27          the workflow help says what the DEFAULT failure policy does
//                  (every step not yet started is cancelled), and Continue's
//                  narrower rule, in one spelling of "dependents";
//   G4-29          the task form's pull-request option speaks of one task, not
//                  "one per step".
//
// G4-25 (the issue reference parsed before it is sent) is in
// intake.issue.test.tsx, beside the form's other cases and its stubs.
//
// Rendered against the development fixture.

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createElement } from 'react'

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, loadTasks: vi.fn(actual.loadTasks) }
})

import * as api from '../api'
import { App } from '../App'
import { DispatchChoice } from '../Dispatch'
import { HELP } from '../help'
import { SubmitChooser } from '../SubmitChooser'
import { SubmitWorkflowScreen, priorityProblem } from '../SubmitWorkflow'
import { STRATEGY_LABEL, consequenceOf } from '../types'

const WAIT = { timeout: 4000 }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
const AN_AGE = /\d+ (s|min|h|d)\b/

describe('G1-05 / G4-23: the chooser times the read it makes', () => {
  it('draws no "reads nothing" in the frame, and its refresh carries the age of the recent read', async () => {
    window.history.replaceState(null, '', '/submit')
    render(<App />)
    await screen.findByRole('heading', { name: 'Submit' }, WAIT)
    const head = document.querySelector<HTMLElement>('main.work .c-phead')!
    await waitFor(() => expect(document.querySelector('.sb-recent [aria-busy="true"]')).toBeNull(), WAIT)
    expect(visible(document.body), 'the head still says it reads nothing').not.toContain('reads nothing')
    const refresh = head.querySelector<HTMLButtonElement>('.c-refresh')
    expect(refresh, 'no refresh control in the chooser head').not.toBeNull()
    await waitFor(() => expect(visible(refresh)).toMatch(AN_AGE), WAIT)
    // One age per screen: the control claims it, so the frame prints none.
    expect(head.querySelector('.ctl-head-age'), 'the frame prints a second age beside the control').toBeNull()
  })

  it('reads the recent submissions again when the refresh is pressed', async () => {
    const { container } = render(<SubmitChooser go={() => {}} />)
    await waitFor(() => expect(container.querySelector('.sb-recent [aria-busy="true"]')).toBeNull(), WAIT)
    const before = vi.mocked(api.loadTasks).mock.calls.length
    expect(before, 'the chooser never read the tasks').toBeGreaterThan(0)
    const refresh = container.querySelector<HTMLButtonElement>('.c-phead .c-refresh')!
    expect(refresh).not.toBeNull()
    fireEvent.click(refresh)
    await waitFor(() => expect(vi.mocked(api.loadTasks).mock.calls.length).toBe(before + 1), WAIT)
    // The list stays drawn while it is read again.
    expect(container.querySelectorAll('.sb-recent-i').length).toBeGreaterThan(0)
  })
})

describe('G4-24: the workflow rail names the cause of a refused priority', () => {
  it('is pure: out of range, not a whole number, or nothing wrong', () => {
    expect(priorityProblem('0')).toBeNull()
    expect(priorityProblem('-100')).toBeNull()
    expect(priorityProblem('100')).toBeNull()
    expect(priorityProblem('500')).toBe('out of range (−100 to 100)')
    expect(priorityProblem('-101')).toBe('out of range (−100 to 100)')
    expect(priorityProblem('2.5')).toBe('not a whole number')
    expect(priorityProblem('')).toBe('not a whole number')
  })

  it('says "out of range" in the summary for 500, and "not a whole number" for 2.5', async () => {
    const { container } = render(<SubmitWorkflowScreen />)
    await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
    const panel = container.querySelector<HTMLElement>('.sbf-send')!
    const fact = () => {
      const f = [...panel.querySelectorAll<HTMLElement>('.ctl-fact')].find((x) => x.querySelector('b')?.textContent === 'priority')
      return visible(f ?? null).slice('priority'.length).trim()
    }
    const prio = screen.getByRole('spinbutton', { name: 'Priority' })
    fireEvent.change(prio, { target: { value: '500' } })
    expect(fact()).toBe('out of range (−100 to 100)')
    fireEvent.change(prio, { target: { value: '2.5' } })
    expect(fact()).toBe('not a whole number')
  })
})

describe('G4-27: the workflow help says what the default failure policy does', () => {
  const topic = HELP['workflow-stages']
  const all = [topic.short, ...topic.long].join(' ')

  it('says the default cancels every step not yet started, and Continue only the dependents', () => {
    expect(topic.short).toContain('With the default, a failure cancels every step not yet started; with Continue, only its dependents.')
    expect(all, 'the help still says independent steps carry on under every policy').not.toMatch(/carry on|runs to its own end/)
  })

  it('spells "dependents" one way across the help and the form', async () => {
    for (const t of Object.values(HELP)) {
      expect([t.short, ...t.long].join(' '), `${t.title} says "dependants"`).not.toMatch(/dependant/)
    }
    render(<SubmitWorkflowScreen />)
    await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
    const fails = screen.getByRole('radiogroup', { name: 'If a step fails' })
    expect(visible(fails)).toContain('dependents')
    expect(visible(fails)).not.toContain('dependants')
  })
})

describe('G4-29: the task form speaks of one task, not of steps', () => {
  it('names the pull-request option for a task, with no "per step"', () => {
    const { container } = render(
      createElement(DispatchChoice, {
        draft: { strategy: 'direct-pr', carrier: 'checkpoints', repositoryUrl: '' },
        onChange: () => {},
        steps: 1,
        scale: 'task',
      }),
    )
    const option = container.querySelector<HTMLInputElement>('input[name="dispatch-strategy"][value="direct-pr"]')!
    const label = option.closest('label')!
    expect(visible(label)).not.toMatch(/per step|one per|PR per/i)
    expect(within(label).getByText('Open a pull request')).toBeTruthy()
    expect(visible(label.querySelector('.dsp-count'))).toBe('One pull request, from this task’s branch — none if it changed nothing.')
  })

  it('keeps the per-step wording on the workflow form', () => {
    expect(consequenceOf('direct-pr', 3).headline).toBe('Up to 3 pull requests — one per step.')
    expect(consequenceOf('direct-pr', 1, 'task').headline).not.toMatch(/step/)
    expect(consequenceOf('direct-pr', 1, 'task').pullRequests).toBe(1)
    expect(consequenceOf('direct-pr', 1, 'task').atMost).toBe(true)
    expect(STRATEGY_LABEL['direct-pr']).toBe('A PR per step')
  })
})
